"""配额借贷与到期清偿：出借企业、借入企业与监管协同处理借贷、冻结、归还与违约追偿。

业务状态机
==========
借贷单（QuotaLoan）::

    pending ──双方确认──▶ confirmed ──放款──▶ active ──到期日──▶ overdue
       │                    │                  │                  │
       └──────cancel────────┴────cancel────────┤                  ├─ 归还 ─▶ repaid
                                               │                  │
                                               └──── 归还 ────────┘
    overdue ──监管宣布违约──▶ defaulted ──手动/自动追偿结清──▶ repaid

- pending：一方发起（发起方默认已确认），等待对方确认，不占用任何配额；
- confirmed：双方确认，出借方对应数量从“自由可用”转为交易占用 reserved，
  与企业间订单/竞价报价占用共用同一套账本隔离不变量
  （current ≥ frozen + reserved），履约冻结、交易占用、借贷占用互不可挤占；
- active：已放款。占用配额离开出借方持仓（current/reserved 同减）、
  借入方到账（current 同增），默认在同一事务内自动核销借入方同年度履约
  缺口（先冻结核销、后到账补缴，补扣流水记 loan_deficit_clear 并关联借贷单）；
- overdue：到达到期日仍未足额归还，监管巡检/手动标记；
- defaulted：监管对逾期借贷宣布违约，登记违约欠额快照，等待借入方补足后追偿；
- repaid：足额归还（正常归还 / 逾期归还 / 违约追偿结清），终态；
- cancelled：放款前任一方撤销（confirmed 撤销释放占用），终态。

到期清偿
========
- 归还只能使用借入方自由可用配额（current - frozen - reserved），绝不动用
  其履约冻结与在途交易占用；出借方同额到账，双方各写一条 loan_repay_out/in 流水；
- 支持分次部分归还，累计归还不超过放款量；最后一笔归还后借贷单转 repaid；
- 逾期/违约欠额可由监管手动追偿，或在借入方后续企业间订单交割/竞价结算
  配额到账后，在清缴之后自动追偿（先清缴、后追偿，绝不挪用履约配额）。

并发安全
========
- 借贷单键锁（loan:<id>）串行化同一借贷单的全部状态流转；所有借贷写流程统一
  按全局锁序 account: < auction: < clear: < loan: < order: 加锁，杜绝锁环；
- 确认占用、放款划转、归还/追偿均复用账本原子条件 UPDATE，余额、占用、
  冻结互不挤占；状态流转用“必须处于前置状态”的条件 UPDATE 抢占，
  并发放款/撤销/归还只有一个事务成功；
- 建单、归还/追偿均支持幂等键唯一约束，双击/超时重试/并发请求只生效一次。
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import case, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.ledger import (
    InsufficientBalanceError,
    account_lock_key,
    apply_ledger_delta,
    company_clear_key,
    is_duplicate_submit,
    lock_row_for_write,
    locked_accounts,
    quota_loan_key,
    transactional,
)
from app.models.allowance import AllowanceAccount, AllowanceTransaction
from app.models.company import Company
from app.models.loan import QuotaLoan, QuotaLoanAuditLog, QuotaLoanRepayment
from app.services.quota_service import settle_borrower_deficit_on_loan

# 借贷单状态
PENDING = "pending"
CONFIRMED = "confirmed"
ACTIVE = "active"
OVERDUE = "overdue"
DEFAULTED = "defaulted"
REPAID = "repaid"
CANCELLED = "cancelled"

_PRE_DISBURSE_STATUSES = (PENDING, CONFIRMED)
# 可归还/追偿的存续状态
_OUTSTANDING_STATUSES = (ACTIVE, OVERDUE, DEFAULTED)

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


@dataclass
class Operator:
    """审计操作人（API 层从登录态构造，service 层不感知 HTTP）。"""

    id: int | None
    username: str
    role: str
    ip: str = ""


class LoanError(ValueError):
    """借贷业务规则不满足（状态非法、非参与方、余额不足等）。"""


def _today() -> str:
    return datetime.utcnow().strftime("%Y-%m-%d")


def _num(value) -> float:
    return float(value) if value is not None else 0.0


def _round4(value) -> float:
    return round(float(value), 4)


# --------------------------------------------------------------------------- #
# 审计
# --------------------------------------------------------------------------- #

def write_audit(
    db: Session,
    operator: Operator | None,
    action: str,
    *,
    target_type: str = "",
    target_id: int | None = None,
    loan_id: int | None = None,
    detail: str = "",
    result: str = "success",
    commit: bool = False,
) -> QuotaLoanAuditLog:
    """写入一条借贷权限审计日志。

    业务操作默认 commit=False：审计与业务变更同事务提交（同生共死）；
    越权拒绝等没有业务事务的场景由调用方传 commit=True 立即落库。
    """
    log = QuotaLoanAuditLog(
        operator_id=operator.id if operator else None,
        operator_name=operator.username if operator else "anonymous",
        operator_role=operator.role if operator else "",
        action=action,
        target_type=target_type,
        target_id=target_id,
        loan_id=loan_id,
        detail=(detail or "")[:500],
        result=result,
        ip=operator.ip if operator else "",
    )
    db.add(log)
    db.flush()
    if commit:
        db.commit()
        db.refresh(log)
    return log


# --------------------------------------------------------------------------- #
# 基础查询与校验
# --------------------------------------------------------------------------- #

def _get_loan(db: Session, loan_id: int) -> QuotaLoan:
    loan = db.get(QuotaLoan, loan_id)
    if loan is None:
        raise LoanError("配额借贷单不存在")
    return loan


def _get_account(db: Session, company_id: int, year: int) -> AllowanceAccount:
    account = (
        db.query(AllowanceAccount)
        .filter(AllowanceAccount.company_id == company_id, AllowanceAccount.year == year)
        .first()
    )
    if account is None:
        raise LoanError(f"企业 {company_id} 的 {year} 年度配额账户不存在，请先完成配额分配")
    return account


def _company_name(db: Session, company_id: int) -> str:
    company = db.get(Company, company_id)
    return company.name if company else str(company_id)


def _gen_loan_no(db: Session, year: int) -> str:
    count = db.query(QuotaLoan).filter(QuotaLoan.year == year).count()
    return f"LN{year}{count + 1:06d}"


def _gen_repay_no(db: Session) -> str:
    count = db.query(QuotaLoanRepayment).count()
    return f"LR{count + 1:08d}{uuid.uuid4().hex[:4].upper()}"


def _validate_due_date(due_date: str) -> str:
    due_date = (due_date or "").strip()
    if not _DATE_RE.match(due_date):
        raise LoanError("到期日格式非法，应为 YYYY-MM-DD")
    try:
        datetime.strptime(due_date, "%Y-%m-%d")
    except ValueError:
        raise LoanError("到期日不是有效日期")
    return due_date


def _transit_loan(
    db: Session,
    loan_id: int,
    expected: tuple[str, ...],
    new_status: str,
) -> int:
    """借贷单状态条件 UPDATE：仅前置状态命中时流转，返回影响行数。

    多进程部署下进程锁无法互斥时，由数据库行更新做最后抢占，
    杜绝并发放款/撤销/追偿造成重复划转。
    """
    result = db.execute(
        update(QuotaLoan)
        .where(QuotaLoan.id == loan_id)
        .where(QuotaLoan.status.in_(expected))
        .values(status=new_status)
        .execution_options(synchronize_session=False)
    )
    return result.rowcount


def _lock_keys_for(
    lender_account: AllowanceAccount | None,
    borrower_account: AllowanceAccount | None,
    loan_id: int,
) -> list[str]:
    """收集借贷操作涉及的全部键。

    键名字典序天然满足 account: < auction: < clear: < loan: < order:，
    locked_accounts 内部还会再排序一次，跨企业操作不会形成锁环。
    """
    keys: list[str] = []
    if loan_id:
        keys.append(quota_loan_key(loan_id))
    for account in (lender_account, borrower_account):
        if account is not None:
            keys.append(account_lock_key(account.id))
            keys.append(company_clear_key(account.company_id, account.year))
    return keys


def _add_ledger(
    db: Session,
    account: AllowanceAccount,
    tx_type: str,
    amount: float,
    balance_after: float,
    frozen_after: float,
    reserved_after: float,
    counterparty: str,
    remark: str,
    loan: QuotaLoan,
    tx_date: str,
) -> AllowanceTransaction:
    tx = AllowanceTransaction(
        account_id=account.id,
        company_id=account.company_id,
        tx_type=tx_type,
        amount=round(amount, 4),
        counterparty=counterparty,
        price=round(float(loan.price or 0), 2),
        tx_date=tx_date,
        balance_after=round(balance_after, 4),
        frozen_after=round(frozen_after, 4),
        reserved_after=round(reserved_after, 4),
        loan_id=loan.id,
        remark=remark,
    )
    db.add(tx)
    return tx


# --------------------------------------------------------------------------- #
# 建单
# --------------------------------------------------------------------------- #

def create_loan(
    db: Session,
    lender_id: int,
    borrower_id: int,
    year: int,
    amount: float,
    *,
    price: float = 0.0,
    initiator: str = "lender",
    due_date: str,
    tx_date: str = "",
    remark: str = "",
    idempotency_key: str | None = None,
    auto_clear_deficit: bool = True,
    auto_recover_default: bool = True,
) -> QuotaLoan:
    """发起配额借贷（出借挂出或借入求借）。

    建单阶段不锁定配额；发起方默认已确认，对方确认时才占用出借方配额。
    携带相同 idempotency_key 的重复提交直接返回首笔借贷单。
    """
    if amount <= 0:
        raise LoanError("借贷数量必须为正数")
    if price < 0:
        raise LoanError("约定费用单价不能为负数")
    if initiator not in ("lender", "borrower"):
        raise LoanError("发起方标识非法")
    if lender_id == borrower_id:
        raise LoanError("出借方与借入方不能为同一企业")
    if not db.get(Company, lender_id):
        raise LoanError("出借企业不存在")
    if not db.get(Company, borrower_id):
        raise LoanError("借入企业不存在")
    due_date = _validate_due_date(due_date)

    lender_account = _get_account(db, lender_id, year)
    borrower_account = _get_account(db, borrower_id, year)

    keys = _lock_keys_for(lender_account, borrower_account, 0)
    keys = [k for k in keys if not k.startswith("loan:")]

    with locked_accounts(keys):
        if idempotency_key:
            existing = (
                db.query(QuotaLoan)
                .filter(QuotaLoan.idempotency_key == idempotency_key)
                .first()
            )
            if existing:
                return existing

        try:
            with transactional(db):
                loan = QuotaLoan(
                    loan_no=_gen_loan_no(db, year),
                    year=year,
                    lender_id=lender_id,
                    borrower_id=borrower_id,
                    amount=round(amount, 4),
                    price=round(price, 2),
                    status=PENDING,
                    lender_confirmed=1 if initiator == "lender" else 0,
                    borrower_confirmed=1 if initiator == "borrower" else 0,
                    initiator=initiator,
                    due_date=due_date,
                    tx_date=tx_date or _today(),
                    remark=(remark or "")[:256],
                    idempotency_key=idempotency_key,
                    auto_clear_deficit=1 if auto_clear_deficit else 0,
                    auto_recover_default=1 if auto_recover_default else 0,
                )
                db.add(loan)
                db.flush()
                db.refresh(loan)
        except IntegrityError as exc:
            if idempotency_key and is_duplicate_submit(exc):
                db.rollback()
                existing = (
                    db.query(QuotaLoan)
                    .filter(QuotaLoan.idempotency_key == idempotency_key)
                    .first()
                )
                if existing:
                    return existing
            raise
        return loan


# --------------------------------------------------------------------------- #
# 双方确认 / 撤销 / 放款
# --------------------------------------------------------------------------- #

def confirm_loan(db: Session, loan_id: int, company_id: int) -> QuotaLoan:
    """参与方确认借贷单；双方均确认时原子占用出借方自由可用配额。

    - 已确认方重复确认为幂等空操作；
    - 非参与方调用按业务错误拒绝；
    - 双方确认瞬间校验出借方“current - frozen - reserved”是否足额，
      不足则拒绝（不改变任何状态/余额）。
    """
    loan = _get_loan(db, loan_id)
    if company_id not in (loan.lender_id, loan.borrower_id):
        raise LoanError("无权操作非本企业参与的借贷单")
    if loan.status in (REPAID, CANCELLED, ACTIVE, OVERDUE, DEFAULTED):
        raise LoanError("借贷单已放款或已终结，不能再确认")

    lender_account = _get_account(db, loan.lender_id, loan.year)
    borrower_account = _get_account(db, loan.borrower_id, loan.year)

    with locked_accounts(_lock_keys_for(lender_account, borrower_account, loan.id)):
        loan = _get_loan(db, loan_id)
        if loan.status in (REPAID, CANCELLED, ACTIVE, OVERDUE, DEFAULTED):
            raise LoanError("借贷单已放款或已终结，不能再确认")

        if company_id == loan.lender_id:
            loan.lender_confirmed = 1
        else:
            loan.borrower_confirmed = 1

        if not (loan.lender_confirmed and loan.borrower_confirmed):
            with transactional(db):
                db.flush()
                db.refresh(loan)
            return loan

        if loan.status == CONFIRMED:
            db.refresh(loan)
            return loan

        amount = _round4(loan.amount)
        try:
            with transactional(db):
                db.flush()
                lender_account = lock_row_for_write(db, lender_account.id)
                available = round(
                    float(lender_account.current_balance)
                    - float(lender_account.frozen_balance)
                    - float(lender_account.reserved_balance),
                    4,
                )
                if available < amount:
                    raise InsufficientBalanceError("出借方可用配额不足")

                balance_after, frozen_after, reserved_after = apply_ledger_delta(
                    db, lender_account.id, 0, 0, amount
                )
                borrower_name = _company_name(db, loan.borrower_id)
                _add_ledger(
                    db,
                    lender_account,
                    "loan_reserve",
                    amount,
                    balance_after,
                    frozen_after,
                    reserved_after,
                    borrower_name,
                    f"借贷 {loan.loan_no} 双方确认，出借配额转为交易占用 {amount} 吨",
                    loan,
                    loan.tx_date or _today(),
                )

                loan.status = CONFIRMED
                loan.confirmed_at = datetime.utcnow()
                db.flush()
                db.refresh(loan)
        except InsufficientBalanceError:
            raise LoanError(
                f"出借方自由可用配额不足（需 {amount} 吨，已扣除履约冻结与其他在途占用），"
                "请出借方补充配额后再确认"
            )
        return loan


def cancel_loan(db: Session, loan_id: int, company_id: int, reason: str = "") -> QuotaLoan:
    """撤销借贷单：放款前任一参与方可撤销；confirmed 单释放出借方占用配额。

    对已放款/已撤销借贷单的重复撤销调用幂等返回当前单据，不报错、不重复释放。
    """
    loan = _get_loan(db, loan_id)
    if company_id not in (loan.lender_id, loan.borrower_id):
        raise LoanError("无权操作非本企业参与的借贷单")
    if loan.status in (ACTIVE, OVERDUE, DEFAULTED, REPAID, CANCELLED):
        db.refresh(loan)
        return loan

    lender_account = _get_account(db, loan.lender_id, loan.year)
    borrower_account = _get_account(db, loan.borrower_id, loan.year)

    with locked_accounts(_lock_keys_for(lender_account, borrower_account, loan.id)):
        loan = _get_loan(db, loan_id)
        if loan.status in (ACTIVE, OVERDUE, DEFAULTED, REPAID, CANCELLED):
            db.refresh(loan)
            return loan

        amount = _round4(loan.amount)
        was_confirmed = loan.status == CONFIRMED
        try:
            with transactional(db):
                # 抢占落终态后再操作 ORM 对象：后续原子 UPDATE 会 expire_all，
                # 先写的 ORM 状态赋值可能被丢弃，故状态以条件 UPDATE 为准。
                if _transit_loan(db, loan_id, _PRE_DISBURSE_STATUSES, CANCELLED) != 1:
                    raise LoanError("借贷单状态已变化，撤销失败，请刷新后重试")

                if was_confirmed:
                    lock_row_for_write(db, lender_account.id)
                    balance_after, frozen_after, reserved_after = apply_ledger_delta(
                        db, lender_account.id, 0, 0, -amount
                    )
                    borrower_name = _company_name(db, loan.borrower_id)
                    _add_ledger(
                        db,
                        lender_account,
                        "loan_release",
                        amount,
                        balance_after,
                        frozen_after,
                        reserved_after,
                        borrower_name,
                        f"借贷 {loan.loan_no} 撤销，释放出借占用配额 {amount} 吨",
                        loan,
                        loan.tx_date or _today(),
                    )

                loan = db.get(QuotaLoan, loan_id)
                loan.status = CANCELLED
                loan.cancelled_by = company_id
                loan.cancel_reason = (reason or "").strip()[:256]
                loan.cancelled_at = datetime.utcnow()
                db.flush()
                db.refresh(loan)
        except InsufficientBalanceError:
            raise LoanError("释放出借占用失败，账本状态异常，撤销已回滚")
        return loan


def disburse_loan(db: Session, loan_id: int, company_id: int) -> QuotaLoan:
    """放款：出借方占用配额划转给借入方，双方账户与流水同步落账。

    出借方 current/reserved 同减（占用转为真正出库），借入方 current 同增；
    随后在同一事务内自动核销借入方同年度履约缺口（可由借贷单关闭联动）。
    放款与撤销并发时由状态条件 UPDATE 保证只有一方成功。
    """
    loan = _get_loan(db, loan_id)
    if company_id not in (loan.lender_id, loan.borrower_id):
        raise LoanError("无权操作非本企业参与的借贷单")
    if loan.status in (ACTIVE, OVERDUE, DEFAULTED, REPAID):
        db.refresh(loan)
        return loan
    if loan.status == CANCELLED:
        raise LoanError("借贷单已撤销，不能放款")
    if loan.status != CONFIRMED:
        raise LoanError("借贷单尚未经双方确认，不能放款")

    lender_account = _get_account(db, loan.lender_id, loan.year)
    borrower_account = _get_account(db, loan.borrower_id, loan.year)

    with locked_accounts(_lock_keys_for(lender_account, borrower_account, loan.id)):
        loan = _get_loan(db, loan_id)
        if loan.status in (ACTIVE, OVERDUE, DEFAULTED, REPAID):
            db.refresh(loan)
            return loan
        if loan.status != CONFIRMED:
            raise LoanError("借贷单未处于双方确认状态，不能放款")

        amount = _round4(loan.amount)
        try:
            with transactional(db):
                if _transit_loan(db, loan_id, (CONFIRMED,), ACTIVE) != 1:
                    raise LoanError("借贷单状态已变化，放款失败，请刷新后重试")

                lender_account = lock_row_for_write(db, lender_account.id)
                borrower_account = lock_row_for_write(db, borrower_account.id)
                tx_date = loan.tx_date or _today()

                lender_name = _company_name(db, loan.lender_id)
                borrower_name = _company_name(db, loan.borrower_id)

                # 出借方：占用配额出库（持仓与占用同减，frozen 不变）
                l_bal, l_frz, l_rsv = apply_ledger_delta(
                    db, lender_account.id, -amount, 0, -amount
                )
                _add_ledger(
                    db,
                    lender_account,
                    "loan_deliver_out",
                    amount,
                    l_bal,
                    l_frz,
                    l_rsv,
                    borrower_name,
                    f"借贷 {loan.loan_no} 放款，向{borrower_name}划出 {amount} 吨",
                    loan,
                    tx_date,
                )

                # 借入方：配额到账（不动既有冻结/占用）
                b_bal, b_frz, b_rsv = apply_ledger_delta(
                    db, borrower_account.id, amount, 0, 0
                )
                _add_ledger(
                    db,
                    borrower_account,
                    "loan_deliver_in",
                    amount,
                    b_bal,
                    b_frz,
                    b_rsv,
                    lender_name,
                    f"借贷 {loan.loan_no} 放款，从{lender_name}借入 {amount} 吨",
                    loan,
                    tx_date,
                )

                # 年度配额闭环：放款到账与借入方履约缺口核销在同一事务完成。
                loan.disbursed_amount = amount
                try:
                    settle_borrower_deficit_on_loan(
                        db,
                        loan,
                        tx_date,
                        auto_clear=bool(int(loan.auto_clear_deficit or 0)),
                    )
                except ValueError as exc:
                    raise LoanError(f"放款联动履约核销失败，整笔放款已回滚：{exc}")

                loan.disbursed_at = datetime.utcnow()
                db.flush()
                db.refresh(loan)
        except InsufficientBalanceError:
            raise LoanError("出借方占用状态异常，放款失败并已回滚")
        return loan


# --------------------------------------------------------------------------- #
# 到期归还
# --------------------------------------------------------------------------- #

def _outstanding(loan: QuotaLoan) -> float:
    """未偿余额 = 放款量 − 已归还量。"""
    return round(max(_num(loan.amount) - _num(loan.repaid_amount), 0.0), 4)


def _apply_one_repayment(
    db: Session,
    loan: QuotaLoan,
    amount: float,
    tx_date: str,
    *,
    kind: str,
    source: str,
    operator: Operator | None,
    remark: str,
    idempotency_key: str | None = None,
) -> QuotaLoanRepayment:
    """在调用方已持锁的写事务内执行一笔归还/追偿（借入方出库 → 出借方到账）。"""
    borrower_acc = _get_account(db, loan.borrower_id, loan.year)
    lender_acc = _get_account(db, loan.lender_id, loan.year)
    borrower_acc = lock_row_for_write(db, borrower_acc.id)
    lender_acc = lock_row_for_write(db, lender_acc.id)

    bal, frz, rsv = apply_ledger_delta(db, borrower_acc.id, -amount, 0, 0)
    borrower_name = _company_name(db, loan.borrower_id)
    lender_name = _company_name(db, loan.lender_id)
    out_type = "loan_repay_out"
    in_type = "loan_repay_in"
    out_remark = f"借贷 {loan.loan_no} 到期清偿划出 {amount:g} 吨"
    in_remark = f"借贷 {loan.loan_no} 借入方归回到账 {amount:g} 吨"
    if kind == "recover":
        out_remark = f"借贷 {loan.loan_no} 违约/逾期追偿划出 {amount:g} 吨"
        in_remark = f"借贷 {loan.loan_no} 追偿配额到账 {amount:g} 吨"
    _add_ledger(db, borrower_acc, out_type, amount, bal, frz, rsv, lender_name,
                out_remark, loan, tx_date)
    l_bal, l_frz, l_rsv = apply_ledger_delta(db, lender_acc.id, amount, 0, 0)
    _add_ledger(db, lender_acc, in_type, amount, l_bal, l_frz, l_rsv, borrower_name,
                in_remark, loan, tx_date)

    new_repaid = round(_num(loan.repaid_amount) + amount, 4)
    loan = db.get(QuotaLoan, loan.id)
    outstanding_after = round(_num(loan.amount) - new_repaid, 4)
    new_status = REPAID if outstanding_after <= 1e-9 else loan.status
    values = {"repaid_amount": new_repaid}
    if new_status == REPAID:
        values["status"] = REPAID
        values["repaid_at"] = datetime.utcnow()
    db.execute(
        update(QuotaLoan)
        .where(QuotaLoan.id == loan.id)
        .values(**values)
        .execution_options(synchronize_session=False)
    )
    loan.repaid_amount = new_repaid
    loan.status = new_status

    repayment = QuotaLoanRepayment(
        repay_no=_gen_repay_no(db),
        loan_id=loan.id,
        lender_id=loan.lender_id,
        borrower_id=loan.borrower_id,
        year=loan.year,
        quantity=amount,
        kind=kind,
        source=source,
        operator_id=operator.id if operator else None,
        remark=(remark or "")[:256],
        idempotency_key=idempotency_key,
    )
    db.add(repayment)
    db.flush()
    return repayment


def repay_loan(
    db: Session,
    loan_id: int,
    operator: Operator | None,
    *,
    amount: float | None = None,
    idempotency_key: str | None = None,
) -> QuotaLoanRepayment:
    """借入方/监管手动归还（active/overdue/defaulted 均可）。

    amount 缺省为剩余未偿余额（一次性清偿）；只使用借入方自由可用配额，
    可用不足则拒绝（不做部分扣减，交由监管追偿或到账自动追偿尽力而为）。
    重复请求携带相同幂等键只生效一次。
    """
    loan = _get_loan(db, loan_id)
    if loan.status == CANCELLED:
        raise LoanError("借贷单已撤销，无清偿义务")

    # 幂等命中优先于状态校验：双击/超时重试（首笔已把借贷单还清）直接返回首笔
    # 归还凭据，而不是向客户端报“已足额归还”。
    if idempotency_key:
        existing = (
            db.query(QuotaLoanRepayment)
            .filter(QuotaLoanRepayment.idempotency_key == idempotency_key)
            .first()
        )
        if existing:
            return existing

    if loan.status == REPAID:
        raise LoanError("借贷单已足额归还")
    if loan.status not in _OUTSTANDING_STATUSES:
        raise LoanError("借贷单尚未放款，不能归还")

    lender_account = _get_account(db, loan.lender_id, loan.year)
    borrower_account = _get_account(db, loan.borrower_id, loan.year)

    with locked_accounts(_lock_keys_for(lender_account, borrower_account, loan.id)):
        loan = _get_loan(db, loan_id)
        if idempotency_key:
            existing = (
                db.query(QuotaLoanRepayment)
                .filter(QuotaLoanRepayment.idempotency_key == idempotency_key)
                .first()
            )
            if existing:
                return existing
        outstanding = _outstanding(loan)
        if outstanding <= 1e-9:
            raise LoanError("借贷单不存在待归还余额")

        try:
            with transactional(db):
                # 进入写事务并抢占借入方账户行写锁：随后读到的借贷单/可用余额
                # 在提交前不会被并发归还改动，消除“读未偿余额做预算 → 划转”
                # 之间的 TOCTOU 窗口（并发部分归还累计不得超过放款量）。
                lock_row_for_write(db, borrower_account.id)
                loan = db.get(QuotaLoan, loan_id)
                if loan.status not in _OUTSTANDING_STATUSES:
                    raise LoanError("借贷单状态已变化，不能归还")
                outstanding = _outstanding(loan)
                if outstanding <= 1e-9:
                    raise LoanError("借贷单不存在待归还余额")
                wanted = outstanding if amount is None else _round4(amount)
                if wanted <= 0:
                    raise LoanError("归还数量必须为正数")
                wanted = round(min(wanted, outstanding), 4)

                borrower_acc = db.get(type(borrower_account), borrower_account.id)
                free = round(
                    float(borrower_acc.current_balance)
                    - float(borrower_acc.frozen_balance)
                    - float(borrower_acc.reserved_balance),
                    4,
                )
                if free + 1e-9 < wanted:
                    raise InsufficientBalanceError("借入方自由可用配额不足")
                kind = "normal" if loan.status == ACTIVE else (
                    "recover" if loan.status == DEFAULTED else "overdue"
                )
                repayment = _apply_one_repayment(
                    db,
                    loan,
                    wanted,
                    _today(),
                    kind=kind,
                    source="manual",
                    operator=operator,
                    remark="借入方到期归还" if loan.status != DEFAULTED else "监管手动追偿违约欠额",
                    idempotency_key=idempotency_key,
                )
                db.flush()
                db.refresh(repayment)
        except InsufficientBalanceError:
            raise LoanError(
                f"借入方自由可用配额不足（需 {wanted:g} 吨），不能完成本次归还；"
                "待其补足配额后可由监管追偿或等待后续交易到账自动追偿"
            )
        except IntegrityError as exc:
            if idempotency_key and is_duplicate_submit(exc):
                db.rollback()
                existing = (
                    db.query(QuotaLoanRepayment)
                    .filter(QuotaLoanRepayment.idempotency_key == idempotency_key)
                    .first()
                )
                if existing:
                    return existing
            raise
        return repayment


# --------------------------------------------------------------------------- #
# 监管：逾期标记 / 宣布违约 / 手动追偿 / 自动追偿
# --------------------------------------------------------------------------- #

def mark_overdue_loans(
    db: Session,
    operator: Operator | None,
    *,
    today: str | None = None,
) -> dict:
    """监管巡检：把已到达到期日（due_date <= today）仍未足额归还的在贷单标记逾期。

    纯状态收敛（不动账本），重复执行幂等；已逾期/违约/终结单据不受影响。
    """
    today = today or _today()
    candidates = (
        db.query(QuotaLoan)
        .filter(
            QuotaLoan.status == ACTIVE,
            QuotaLoan.due_date != "",
            QuotaLoan.due_date <= today,
            QuotaLoan.repaid_amount < QuotaLoan.amount,
        )
        .order_by(QuotaLoan.id.asc())
        .all()
    )
    marked: list[QuotaLoan] = []
    for loan in candidates:
        with locked_accounts([quota_loan_key(loan.id)]):
            with transactional(db):
                if _transit_loan(db, loan.id, (ACTIVE,), OVERDUE) == 1:
                    db.execute(
                        update(QuotaLoan)
                        .where(QuotaLoan.id == loan.id)
                        .values(overdue_at=datetime.utcnow())
                        .execution_options(synchronize_session=False)
                    )
                    db.refresh(loan)
                    marked.append(loan)
                    write_audit(
                        db, operator, "overdue.mark",
                        target_type="loan", target_id=loan.id, loan_id=loan.id,
                        detail=f"借贷 {loan.loan_no} 已过到期日 {loan.due_date}，标记逾期",
                    )
    return {"marked": len(marked), "loans": marked}


def declare_default(
    db: Session,
    loan_id: int,
    operator: Operator,
    reason: str,
) -> QuotaLoan:
    """监管对逾期借贷宣布违约：冻结未偿余额快照（defaulted_amount），等待追偿。

    仅 overdue 借贷单可宣布违约；宣布违约本身不划转配额（借入方持仓可能为零，
    欠额由后续手动/自动追偿按其自由可用尽力收回）。
    """
    reason = (reason or "").strip()
    if len(reason) < 2:
        raise LoanError("请填写违约原因（至少 2 个字符）")

    loan = _get_loan(db, loan_id)
    if loan.status == DEFAULTED:
        db.refresh(loan)
        return loan
    if loan.status != OVERDUE:
        raise LoanError("仅已逾期（overdue）的借贷单可宣布违约")

    lender_account = _get_account(db, loan.lender_id, loan.year)
    borrower_account = _get_account(db, loan.borrower_id, loan.year)
    with locked_accounts(_lock_keys_for(lender_account, borrower_account, loan.id)):
        loan = _get_loan(db, loan_id)
        if loan.status == DEFAULTED:
            db.refresh(loan)
            return loan
        if loan.status != OVERDUE:
            raise LoanError("仅已逾期（overdue）的借贷单可宣布违约")

        with transactional(db):
            outstanding = _outstanding(loan)
            if outstanding <= 1e-9:
                raise LoanError("借贷单已无未偿余额，不能宣布违约")
            if _transit_loan(db, loan_id, (OVERDUE,), DEFAULTED) != 1:
                raise LoanError("借贷单状态已变化，宣布违约失败，请刷新后重试")
            db.execute(
                update(QuotaLoan)
                .where(QuotaLoan.id == loan_id)
                .values(defaulted_amount=outstanding,
                        default_reason=reason[:500],
                        defaulted_at=datetime.utcnow())
                .execution_options(synchronize_session=False)
            )
            loan.defaulted_amount = outstanding
            loan.default_reason = reason[:500]
            loan.status = DEFAULTED
            write_audit(
                db, operator, "loan.default",
                target_type="loan", target_id=loan_id, loan_id=loan_id,
                detail=f"借贷 {loan.loan_no} 宣布违约，未偿欠额 {outstanding:g} 吨：{reason}",
            )
            db.flush()
            db.refresh(loan)
        return loan


def overdue_loan_recovery_keys(db: Session, borrower_id: int, year: int) -> list[str]:
    """收集借入方全部待追偿（defaulted/overdue 且开启自动追偿）借贷单的键。

    供订单交割/竞价结算在开启事务**之前**调用，把借贷键并入统一锁集合：
    键名字典序满足 account: < auction: < clear: < loan: < order:，
    与业务主流程按同一全局锁序加锁，不会形成锁环。
    """
    loans = (
        db.query(QuotaLoan)
        .filter(
            QuotaLoan.borrower_id == borrower_id,
            QuotaLoan.year == year,
            QuotaLoan.status.in_([DEFAULTED, OVERDUE]),
            QuotaLoan.auto_recover_default == 1,
            QuotaLoan.repaid_amount < QuotaLoan.amount,
        )
        .all()
    )
    keys = {quota_loan_key(loan.id) for loan in loans}
    # 追偿划付会触碰到出借方账户，一并取其账户/清缴键
    for loan in loans:
        acc = _get_account(db, loan.lender_id, year)
        keys.add(account_lock_key(acc.id))
        keys.add(company_clear_key(loan.lender_id, year))
    return sorted(keys)


def _recoverable_loans_for_borrower(db: Session, borrower_id: int, year: int) -> list[QuotaLoan]:
    """借入方待追偿借贷单：违约优先，其次逾期；按 id（时间）升序。"""
    loans = (
        db.query(QuotaLoan)
        .filter(
            QuotaLoan.borrower_id == borrower_id,
            QuotaLoan.year == year,
            QuotaLoan.status.in_([DEFAULTED, OVERDUE]),
            QuotaLoan.auto_recover_default == 1,
            QuotaLoan.repaid_amount < QuotaLoan.amount,
        )
        .order_by(
            # defaulted 优先追偿（已构成违约敞口），其次 overdue，再按时间
            case((QuotaLoan.status == DEFAULTED, 0), else_=1),
            QuotaLoan.id.asc(),
        )
        .all()
    )
    return loans


def auto_recover_borrower_loans(
    db: Session,
    borrower_id: int,
    year: int,
    tx_date: str,
    *,
    operator: Operator | None = None,
) -> dict:
    """用借入方自由可用配额自动追偿其全部逾期/违约借贷欠额。

    必须在调用方（订单交割/竞价结算）已持全部相关锁并开启写事务后调用，
    因此这里不再加锁、不自行开启事务：追偿与主交易同生共死。
    先清缴后追偿：仅使用 current - frozen - reserved，逐笔按违约优先、
    时间优先偿还；无欠额/无可用为空操作。
    """
    loans = _recoverable_loans_for_borrower(db, borrower_id, year)
    if not loans:
        return {"repayments": [], "recovered": 0.0}

    borrower_acc = lock_row_for_write(db, _get_account(db, borrower_id, year).id)
    free = round(
        float(borrower_acc.current_balance)
        - float(borrower_acc.frozen_balance)
        - float(borrower_acc.reserved_balance),
        4,
    )
    budget = max(free, 0.0)
    repayments: list[QuotaLoanRepayment] = []
    recovered_total = 0.0

    for loan in loans:
        if budget <= 1e-9:
            break
        outstanding = _outstanding(loan)
        if outstanding <= 1e-9:
            continue
        pay = round(min(outstanding, budget), 4)
        if pay <= 0:
            continue
        lock_row_for_write(db, _get_account(db, loan.lender_id, year).id)
        repayment = _apply_one_repayment(
            db,
            loan,
            pay,
            tx_date,
            kind="recover",
            source="auto",
            operator=operator,
            remark="后续配额到账自动追偿借贷欠额",
        )
        write_audit(
            db, operator, "loan.auto_recover",
            target_type="loan", target_id=loan.id, loan_id=loan.id,
            detail=f"借入方 {_company_name(db, borrower_id)} 到账后自动追偿借贷 "
                   f"{loan.loan_no} {pay:g} 吨",
        )
        repayments.append(repayment)
        budget = round(budget - pay, 4)
        recovered_total = round(recovered_total + pay, 4)

    return {"repayments": repayments, "recovered": recovered_total}


def recover_borrower_loans(
    db: Session,
    borrower_id: int,
    year: int,
    operator: Operator,
) -> dict:
    """监管手动触发：对借入方某年度全部逾期/违约欠额执行追偿（自由可用不足则尽力而为）。"""
    loans = _recoverable_loans_for_borrower(db, borrower_id, year)
    if not loans:
        raise LoanError("该企业该年度没有待追偿的逾期/违约借贷欠额")

    keys = [
        account_lock_key(_get_account(db, borrower_id, year).id),
        company_clear_key(borrower_id, year),
    ]
    for loan in loans:
        keys.append(quota_loan_key(loan.id))
        keys.append(account_lock_key(_get_account(db, loan.lender_id, year).id))
        keys.append(company_clear_key(loan.lender_id, year))

    result = {"repayments": [], "recovered": 0.0}
    with locked_accounts(sorted(set(keys))):
        try:
            with transactional(db):
                # 手动追偿不要求借贷单开启自动追偿开关：监管可对任意逾期/违约单追偿
                all_loans = (
                    db.query(QuotaLoan)
                    .filter(
                        QuotaLoan.borrower_id == borrower_id,
                        QuotaLoan.year == year,
                        QuotaLoan.status.in_([DEFAULTED, OVERDUE]),
                        QuotaLoan.repaid_amount < QuotaLoan.amount,
                    )
                    .order_by(
                        case((QuotaLoan.status == DEFAULTED, 0), else_=1),
                        QuotaLoan.id.asc(),
                    )
                    .all()
                )
                borrower_acc = lock_row_for_write(db, _get_account(db, borrower_id, year).id)
                budget = max(round(
                    float(borrower_acc.current_balance)
                    - float(borrower_acc.frozen_balance)
                    - float(borrower_acc.reserved_balance),
                    4,
                ), 0.0)
                tx_date = _today()
                for loan in all_loans:
                    if budget <= 1e-9:
                        break
                    outstanding = _outstanding(loan)
                    pay = round(min(outstanding, budget), 4)
                    if pay <= 0:
                        continue
                    lock_row_for_write(db, _get_account(db, loan.lender_id, year).id)
                    repayment = _apply_one_repayment(
                        db, loan, pay, tx_date,
                        kind="recover", source="manual", operator=operator,
                        remark="监管手动追偿逾期/违约借贷欠额",
                    )
                    write_audit(
                        db, operator, "loan.recover",
                        target_type="loan", target_id=loan.id, loan_id=loan.id,
                        detail=f"监管手动追偿借贷 {loan.loan_no} {pay:g} 吨",
                    )
                    result["repayments"].append(repayment)
                    budget = round(budget - pay, 4)
                    result["recovered"] = round(result["recovered"] + pay, 4)
                db.flush()
                for r in result["repayments"]:
                    db.refresh(r)
        except InsufficientBalanceError:
            raise LoanError("借贷追偿划转失败，账本状态异常，操作已回滚")
        if not result["repayments"]:
            raise LoanError("借入方自由可用配额不足，暂无可追偿配额，请待其补足后重试")
        return result


# --------------------------------------------------------------------------- #
# 查询辅助
# --------------------------------------------------------------------------- #

def list_loans(
    db: Session,
    *,
    company_id: int | None = None,
    year: int | None = None,
    status: str | None = None,
) -> list[QuotaLoan]:
    q = db.query(QuotaLoan)
    if company_id is not None:
        q = q.filter(
            (QuotaLoan.lender_id == company_id) | (QuotaLoan.borrower_id == company_id)
        )
    if year is not None:
        q = q.filter(QuotaLoan.year == year)
    if status:
        q = q.filter(QuotaLoan.status == status)
    return q.order_by(QuotaLoan.id.desc()).all()


def list_overdue_loans(
    db: Session,
    *,
    borrower_id: int | None = None,
    year: int | None = None,
) -> list[QuotaLoan]:
    """待追偿的逾期/违约借贷单（监管全部；借入方仅本企业）。"""
    q = db.query(QuotaLoan).filter(
        QuotaLoan.status.in_([OVERDUE, DEFAULTED]),
        QuotaLoan.repaid_amount < QuotaLoan.amount,
    )
    if borrower_id is not None:
        q = q.filter(QuotaLoan.borrower_id == borrower_id)
    if year is not None:
        q = q.filter(QuotaLoan.year == year)
    return q.order_by(QuotaLoan.id.asc()).all()


def list_repayments(
    db: Session,
    *,
    loan_id: int | None = None,
    borrower_id: int | None = None,
) -> list[QuotaLoanRepayment]:
    q = db.query(QuotaLoanRepayment)
    if loan_id is not None:
        q = q.filter(QuotaLoanRepayment.loan_id == loan_id)
    if borrower_id is not None:
        q = q.filter(QuotaLoanRepayment.borrower_id == borrower_id)
    return q.order_by(QuotaLoanRepayment.id.asc()).all()


def list_audit_logs(
    db: Session,
    *,
    loan_id: int | None = None,
    limit: int = 200,
) -> list[QuotaLoanAuditLog]:
    q = db.query(QuotaLoanAuditLog)
    if loan_id is not None:
        q = q.filter(QuotaLoanAuditLog.loan_id == loan_id)
    return q.order_by(QuotaLoanAuditLog.id.desc()).limit(limit).all()
