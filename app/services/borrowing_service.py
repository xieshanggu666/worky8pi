"""企业间配额拆借（配额借贷）：协商冻结、监管拨付、到期归还与违约追偿。

业务状态机
==========
借据（QuotaLoan）::

    pending ──双方确认──▶ pending（仅确认标志变化）
    pending ──监管批准（双方已确认，冻结出借方自由可用）──▶ reserved
    pending ──监管驳回──▶ rejected
    pending ──任一方撤销 / 监管撤销──▶ cancelled（无账本副作用）
    reserved ──监管拨付（冻结出库、借入方到账、联动清缴）──▶ active
    reserved ──监管撤销──▶ cancelled（释放借贷冻结）
    active  ──到期日届满监管标记──▶ overdue
    active/overdue ──足额归还──▶ repaid
    overdue ──监管宣告违约（尽力收回，不足登记欠额）──▶ defaulted
    defaulted ──手动追偿 / 借入方后续到账自动追偿结清──▶ repaid

记账与占用
==========
- 批准冻结复用账户 ``reserved_balance``（与企业间订单/竞价报价同一套
  ``current ≥ frozen + reserved`` 原子约束）：出借方被冻结的配额既不能
  再被卖出/报卖/二次出借，也不能被履约清缴挪用，借入方在拨付前拿不到配额；
- 拨付：出借方 current/reserved 同减（loan_disburse_out）、借入方 current
  同增（loan_disburse_in），同事务核销借入方同年度履约缺口
  （loan_deficit_clear，借据归属，回退/报告冲正按归属精确退还）；
- 归还/追偿：借入方出库（loan_repay_out / loan_default_*_out）与出借方
  到账（loan_repay_in / …_in）跨主体严格配对，系统总配额守恒；
- 宣告违约只从借入方自由可用尽力收回，不足部分登记为表外违约欠额
  （defaulted_amount），欠额不豁免其履约义务，待其补足配额后追偿。

并发安全
========
- 借据键锁（``loan:<id>``，锁序 clear < loan < order）串行化同一借据的全部
  写操作；借据自动追偿可能被订单交割、竞价结算嵌套触发，外层统一按
  键名字典序取锁（account < auction < clear < loan < order），不形成锁环；
- 批准/拨付/撤销/宣告违约均以“状态必须为前置状态”的条件 UPDATE 抢占，
  并发请求只有一方成功，其余幂等返回或拒绝；
- 建单、归还/追偿均支持幂等键；双击 / 超时重试 / HTTP 并发只生效一次。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import update
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
from app.models.borrowing import (
    QuotaLoan,
    QuotaLoanAuditLog,
    QuotaLoanRepayment,
)
from app.models.company import Company
from app.services.quota_service import settle_borrower_deficit_on_loan

# 借据状态
PENDING = "pending"
RESERVED = "reserved"
ACTIVE = "active"
OVERDUE = "overdue"
DEFAULTED = "defaulted"
REPAID = "repaid"
REJECTED = "rejected"
CANCELLED = "cancelled"

# 归还/追偿来源
SRC_REPAY = "repay"
SRC_CLAWBACK = "default_clawback"
SRC_MANUAL = "manual_recovery"
SRC_AUTO = "auto_recovery"

_TERMINAL_STATUSES = (REPAID, REJECTED, CANCELLED)


@dataclass
class Operator:
    """审计操作人（API 层从登录态构造，service 层不感知 HTTP）。"""

    id: int | None
    username: str
    role: str
    ip: str = ""


def _today() -> str:
    return datetime.utcnow().strftime("%Y-%m-%d")


def _num(value) -> float:
    return float(value) if value is not None else 0.0


def _round4(value) -> float:
    return round(float(value), 4)


class BorrowingError(ValueError):
    """借贷业务规则不满足（状态非法、非参与方、余额不足、越权等）。"""


# --------------------------------------------------------------------------- #
# 审计
# --------------------------------------------------------------------------- #

def write_audit(
    db: Session,
    operator: Operator | None,
    action: str,
    *,
    target_id: int | None = None,
    loan_id: int | None = None,
    detail: str = "",
    result: str = "success",
    commit: bool = False,
) -> QuotaLoanAuditLog:
    """写一条借贷权限审计日志（默认与业务事务同生共死；越权拒绝立即提交）。"""
    log = QuotaLoanAuditLog(
        operator_id=operator.id if operator else None,
        operator_name=operator.username if operator else "anonymous",
        operator_role=operator.role if operator else "",
        action=action,
        target_type="loan",
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
# 基础查询/编号
# --------------------------------------------------------------------------- #

def _get_loan(db: Session, loan_id: int) -> QuotaLoan:
    loan = db.get(QuotaLoan, loan_id)
    if loan is None:
        raise BorrowingError("配额借据不存在")
    return loan


def _get_account(db: Session, company_id: int, year: int) -> AllowanceAccount:
    account = (
        db.query(AllowanceAccount)
        .filter(AllowanceAccount.company_id == company_id, AllowanceAccount.year == year)
        .first()
    )
    if account is None:
        raise BorrowingError(
            f"企业 {company_id} 的 {year} 年度配额账户不存在，请先完成配额分配"
        )
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


def _transit_loan(
    db: Session,
    loan_id: int,
    expected: tuple[str, ...],
    new_status: str,
) -> int:
    """借据状态条件 UPDATE：仅前置状态命中时流转，返回影响行数。"""
    result = db.execute(
        update(QuotaLoan)
        .where(QuotaLoan.id == loan_id)
        .where(QuotaLoan.status.in_(expected))
        .values(status=new_status)
        .execution_options(synchronize_session=False)
    )
    return result.rowcount


def _restart_read_snapshot(db: Session) -> None:
    """取锁后结束加锁前可能已开启的只读事务，丢弃身份映射旧快照。

    多个会话并发时，先到请求在等待进程键锁之前可能已 SELECT 借据，SQLite
    读事务持有旧快照；获锁后若直接用身份映射中的旧对象计算未偿本金，会把
    已被并发请求归还的数量再算一次（超付）。顶层写入口获锁后统一调用本函数：
    此时本会话尚无任何待提交写入，回滚只读事务无副作用，随后重读拿到锁内最新值。

    注意：被订单交割/竞价结算嵌套调用的自动追偿不得调用——它运行在外层业务
    事务中，回滚会连带撤销外层已做的划转。
    """
    db.rollback()


def _bump_loan_returned(
    db: Session,
    loan_id: int,
    pay: float,
    *,
    new_recovered: float | None = None,
    new_status_if_complete: str | None = REPAID,
) -> QuotaLoan:
    """条件 UPDATE 累加借据归还/追偿量，数据库层兜底“累计归还不超过本金”。

    多进程部署下进程键锁无法互斥时，由带
    ``returned_amount + pay <= amount`` 条件的原子 UPDATE 兜底防超付；
    影响行数为 0 即说明并发下本金已被他请求还清，调用方应跳过本笔。
    """
    returned_expr = QuotaLoan.returned_amount + pay
    values: dict = {"returned_amount": returned_expr}
    stmt = (
        update(QuotaLoan)
        .where(QuotaLoan.id == loan_id)
        .where(returned_expr <= QuotaLoan.amount)
    )
    if new_recovered is not None:
        values["recovered_default"] = new_recovered
    if new_status_if_complete is not None:
        # SQL 层无法引用别名表达式做条件，状态由调用方按重读结果补写
        pass
    result = db.execute(stmt.values(**values).execution_options(synchronize_session=False))
    if result.rowcount != 1:
        raise InsufficientBalanceError("借据本金已被并发请求清偿，本笔归还被数据库拒绝")
    db.expire_all()
    loan = db.get(QuotaLoan, loan_id)
    complete = _num(loan.returned_amount) + 1e-9 >= _num(loan.amount)
    if new_recovered is not None:
        complete = complete and _num(loan.recovered_default) + 1e-9 >= _num(loan.defaulted_amount)
    if complete and new_status_if_complete is not None:
        loan.status = new_status_if_complete
        loan.closed_at = datetime.utcnow()
    return loan


def _free_balance(account: AllowanceAccount) -> float:
    """自由可用余额 = 持仓 − 履约冻结 − 交易/借贷占用。"""
    return round(
        _num(account.current_balance)
        - _num(account.frozen_balance)
        - _num(account.reserved_balance),
        4,
    )


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
        price=None,
        tx_date=tx_date,
        balance_after=round(balance_after, 4),
        frozen_after=round(frozen_after, 4),
        reserved_after=round(reserved_after, 4),
        quota_loan_id=loan.id,
        remark=remark,
    )
    db.add(tx)
    return tx


def _outstanding_principal(loan: QuotaLoan) -> float:
    """未归还本金 = 拆借量 − 累计已归还。"""
    return round(_num(loan.amount) - _num(loan.returned_amount), 4)


def _outstanding_default(loan: QuotaLoan) -> float:
    """未追偿违约欠额 = 违约登记 − 已追偿。"""
    return round(_num(loan.defaulted_amount) - _num(loan.recovered_default), 4)


# --------------------------------------------------------------------------- #
# 借据协商：创建 / 确认 / 撤销 / 监管驳回
# --------------------------------------------------------------------------- #

def create_loan(
    db: Session,
    *,
    lender_id: int,
    borrower_id: int,
    year: int,
    amount: float,
    due_date: str,
    interest_rate: float = 0.0,
    initiator: str = "lender",
    tx_date: str = "",
    remark: str = "",
    auto_clear_deficit: bool = True,
    auto_recover_default: bool = True,
    operator: Operator | None = None,
    idempotency_key: str | None = None,
) -> QuotaLoan:
    """企业发起配额拆借协商（发起方默认已确认）。挂单阶段不动账。

    携带相同幂等键的重复提交（双击/超时重试）返回首张借据。
    """
    if amount <= 0:
        raise BorrowingError("拆借数量必须为正数")
    if interest_rate < 0:
        raise BorrowingError("约定费率不能为负数")
    if initiator not in ("lender", "borrower"):
        raise BorrowingError("发起方标识非法")
    if lender_id == borrower_id:
        raise BorrowingError("出借方与借入方不能为同一企业")
    if not db.get(Company, lender_id):
        raise BorrowingError("出借方企业不存在")
    if not db.get(Company, borrower_id):
        raise BorrowingError("借入方企业不存在")
    due_date = (due_date or "").strip()
    if not due_date:
        raise BorrowingError("请填写约定归还日期")

    lender_account = _get_account(db, lender_id, year)
    borrower_account = _get_account(db, borrower_id, year)
    keys = sorted({
        account_lock_key(lender_account.id),
        account_lock_key(borrower_account.id),
        company_clear_key(lender_id, year),
        company_clear_key(borrower_id, year),
    })

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
                    interest_rate=round(interest_rate, 4),
                    due_date=due_date,
                    status=PENDING,
                    lender_confirmed=1 if initiator == "lender" else 0,
                    borrower_confirmed=1 if initiator == "borrower" else 0,
                    initiator=initiator,
                    auto_clear_deficit=1 if auto_clear_deficit else 0,
                    auto_recover_default=1 if auto_recover_default else 0,
                    tx_date=tx_date or _today(),
                    remark=(remark or "").strip()[:256],
                    created_by=operator.id if operator else None,
                    idempotency_key=idempotency_key,
                )
                db.add(loan)
                db.flush()
                write_audit(
                    db, operator,
                    "loan.create", target_id=loan.id, loan_id=loan.id,
                    detail=f"企业 {_company_name(db, lender_id)} 向 "
                    f"{_company_name(db, borrower_id)} 发起拆借 {amount:g} 吨"
                    f"（{year} 年度，约定归还日 {due_date}），借据 {loan.loan_no}",
                )
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


def confirm_loan(db: Session, loan_id: int, company_id: int) -> QuotaLoan:
    """出借方/借入方确认借据协商。已确认方重复确认为幂等空操作。"""
    loan = _get_loan(db, loan_id)
    if company_id not in (loan.lender_id, loan.borrower_id):
        raise BorrowingError("无权操作非本企业参与的借据")
    if loan.status in (REJECTED, CANCELLED):
        raise BorrowingError("借据已被驳回或撤销，不能再确认")
    if loan.status != PENDING:
        raise BorrowingError(f"借据当前为 {loan.status}，无需再确认")

    lender_acc = _get_account(db, loan.lender_id, loan.year)
    borrower_acc = _get_account(db, loan.borrower_id, loan.year)
    keys = sorted({
        quota_loan_key(loan_id),
        account_lock_key(lender_acc.id),
        account_lock_key(borrower_acc.id),
        company_clear_key(loan.lender_id, loan.year),
        company_clear_key(loan.borrower_id, loan.year),
    })
    with locked_accounts(keys):
        _restart_read_snapshot(db)
        loan = _get_loan(db, loan_id)
        if loan.status != PENDING:
            db.refresh(loan)
            return loan
        with transactional(db):
            if company_id == loan.lender_id:
                loan.lender_confirmed = 1
            else:
                loan.borrower_confirmed = 1
            if loan.lender_confirmed and loan.borrower_confirmed and not loan.confirmed_at:
                loan.confirmed_at = datetime.utcnow()
            db.flush()
            db.refresh(loan)
        return loan


def cancel_loan(
    db: Session,
    loan_id: int,
    *,
    company_id: int | None = None,
    operator: Operator | None = None,
    reason: str = "",
) -> QuotaLoan:
    """撤销借据。

    - pending：任一参与方企业或监管可撤销（无账本副作用）；
    - reserved：仅监管可撤销，释放出借方借贷冻结（loan_release）；
    - 终态/已拨付借据不可撤销；重复撤销幂等返回。
    """
    loan = _get_loan(db, loan_id)
    as_regulator = company_id is None
    if not as_regulator and company_id not in (loan.lender_id, loan.borrower_id):
        raise BorrowingError("无权撤销非本企业参与的借据")
    if loan.status in _TERMINAL_STATUSES:
        db.refresh(loan)
        return loan
    if not as_regulator and loan.status == RESERVED:
        raise BorrowingError("借据已由监管批准冻结，企业不能自行撤销，请申请监管撤销")
    if loan.status not in (PENDING, RESERVED):
        raise BorrowingError(f"借据当前为 {loan.status}，不能撤销")

    lender_acc = _get_account(db, loan.lender_id, loan.year)
    borrower_acc = _get_account(db, loan.borrower_id, loan.year)
    keys = sorted({
        quota_loan_key(loan_id),
        account_lock_key(lender_acc.id),
        account_lock_key(borrower_acc.id),
        company_clear_key(loan.lender_id, loan.year),
        company_clear_key(loan.borrower_id, loan.year),
    })
    with locked_accounts(keys):
        _restart_read_snapshot(db)
        loan = _get_loan(db, loan_id)
        if loan.status in _TERMINAL_STATUSES:
            db.refresh(loan)
            return loan

        reason = (reason or "").strip()[:256]
        try:
            with transactional(db):
                if _transit_loan(db, loan_id, (PENDING, RESERVED), CANCELLED) != 1:
                    raise BorrowingError("借据状态已变化，撤销失败，请刷新后重试")

                if loan.status == RESERVED:
                    amount = _round4(loan.amount)
                    lender_acc = lock_row_for_write(db, lender_acc.id)
                    bal, frz, rsv = apply_ledger_delta(db, lender_acc.id, 0, 0, -amount)
                    _add_ledger(
                        db, lender_acc, "loan_release", amount,
                        bal, frz, rsv, "监管撤销",
                        f"借据 {loan.loan_no} 监管撤销，释放借贷冻结 {amount} 吨",
                        loan, _today(),
                    )

                loan = _get_loan(db, loan_id)
                loan.cancel_reason = reason
                loan.cancelled_by = 0 if as_regulator else company_id
                loan.cancelled_at = datetime.utcnow()
                write_audit(
                    db, operator,
                    "loan.cancel", target_id=loan_id, loan_id=loan_id,
                    detail=("监管" if as_regulator else f"企业 {company_id}")
                    + f"撤销借据 {loan.loan_no}：{reason or '（无原因）'}"
                    + ("，已释放借贷冻结" if loan.reserved_at else ""),
                )
                db.flush()
                db.refresh(loan)
        except InsufficientBalanceError:
            raise BorrowingError("释放借贷冻结失败，账本状态异常，撤销已回滚")
        return loan


def reject_loan(
    db: Session,
    loan_id: int,
    operator: Operator | None,
    reason: str,
) -> QuotaLoan:
    """监管驳回尚在协商（pending）的借据。无账本副作用，状态终态。"""
    reason = (reason or "").strip()
    if len(reason) < 2:
        raise BorrowingError("请填写驳回原因（至少 2 个字符）")
    loan = _get_loan(db, loan_id)
    if loan.status in (REJECTED, CANCELLED):
        db.refresh(loan)
        return loan
    if loan.status != PENDING:
        raise BorrowingError(f"借据当前为 {loan.status}，不能驳回")

    with locked_accounts([quota_loan_key(loan_id)]):
        _restart_read_snapshot(db)
        loan = _get_loan(db, loan_id)
        if loan.status in (REJECTED, CANCELLED):
            db.refresh(loan)
            return loan
        with transactional(db):
            if _transit_loan(db, loan_id, (PENDING,), REJECTED) != 1:
                raise BorrowingError("借据状态已变化，驳回失败，请刷新后重试")
            loan = _get_loan(db, loan_id)
            loan.reject_reason = reason[:256]
            loan.rejected_by = operator.id if operator else None
            loan.rejected_at = datetime.utcnow()
            write_audit(
                db, operator, "loan.reject",
                target_id=loan_id, loan_id=loan_id,
                detail=f"监管驳回借据 {loan.loan_no}：{reason}",
            )
            db.flush()
            db.refresh(loan)
        return loan


# --------------------------------------------------------------------------- #
# 监管批准（冻结出借方配额）与拨付
# --------------------------------------------------------------------------- #

def approve_loan(db: Session, loan_id: int, operator: Operator | None) -> QuotaLoan:
    """监管批准借据：双方必须已确认，随即冻结出借方自由可用配额（reserved）。

    出借方自由可用不足时拒绝（不改变任何状态），待其补充配额或撤销借据后重试。
    """
    loan = _get_loan(db, loan_id)
    if loan.status == RESERVED:
        db.refresh(loan)
        return loan
    if loan.status != PENDING:
        raise BorrowingError(f"借据当前为 {loan.status}，不能批准冻结")
    if not (loan.lender_confirmed and loan.borrower_confirmed):
        raise BorrowingError("借贷双方尚未全部确认借据，不能批准")

    lender_acc = _get_account(db, loan.lender_id, loan.year)
    borrower_acc = _get_account(db, loan.borrower_id, loan.year)
    keys = sorted({
        quota_loan_key(loan_id),
        account_lock_key(lender_acc.id),
        account_lock_key(borrower_acc.id),
        company_clear_key(loan.lender_id, loan.year),
        company_clear_key(loan.borrower_id, loan.year),
    })
    with locked_accounts(keys):
        _restart_read_snapshot(db)
        loan = _get_loan(db, loan_id)
        if loan.status == RESERVED:
            db.refresh(loan)
            return loan
        if loan.status != PENDING:
            raise BorrowingError("借据已离开协商状态，不能批准冻结")

        amount = _round4(loan.amount)
        try:
            with transactional(db):
                if _transit_loan(db, loan_id, (PENDING,), RESERVED) != 1:
                    raise BorrowingError("借据状态已变化，批准失败，请刷新后重试")
                lender_acc = lock_row_for_write(db, lender_acc.id)
                available = _free_balance(lender_acc)
                if available + 1e-9 < amount:
                    raise InsufficientBalanceError("出借方自由可用配额不足")
                bal, frz, rsv = apply_ledger_delta(db, lender_acc.id, 0, 0, amount)
                borrower_name = _company_name(db, loan.borrower_id)
                _add_ledger(
                    db, lender_acc, "loan_reserve", amount,
                    bal, frz, rsv, borrower_name,
                    f"借据 {loan.loan_no} 监管批准，冻结出借配额 {amount} 吨",
                    loan, _today(),
                )
                loan = _get_loan(db, loan_id)
                loan.approved_by = operator.id if operator else None
                loan.reserved_at = datetime.utcnow()
                write_audit(
                    db, operator, "loan.approve",
                    target_id=loan_id, loan_id=loan_id,
                    detail=f"监管批准借据 {loan.loan_no}，冻结出借方 "
                    f"{_company_name(db, loan.lender_id)} 自由可用配额 {amount:g} 吨",
                )
                db.flush()
                db.refresh(loan)
        except InsufficientBalanceError:
            raise BorrowingError(
                f"出借方自由可用配额不足（需 {amount:g} 吨，已扣除履约冻结与全部占用），"
                "请待其补充配额后再批准"
            )
        return loan


def disburse_loan(db: Session, loan_id: int, operator: Operator | None) -> QuotaLoan:
    """监管拨付已冻结借据：冻结配额出库、借入方到账、同事务核销其履约缺口。

    拨付是借据生效动作：拨付前借据可由监管撤销并释放冻结，拨付后只能归还/追偿。
    """
    loan = _get_loan(db, loan_id)
    if loan.status == ACTIVE:
        db.refresh(loan)
        return loan
    if loan.status != RESERVED:
        raise BorrowingError(f"借据当前为 {loan.status}，须先批准冻结后才能拨付")

    lender_acc = _get_account(db, loan.lender_id, loan.year)
    borrower_acc = _get_account(db, loan.borrower_id, loan.year)
    keys = sorted({
        quota_loan_key(loan_id),
        account_lock_key(lender_acc.id),
        account_lock_key(borrower_acc.id),
        company_clear_key(loan.lender_id, loan.year),
        company_clear_key(loan.borrower_id, loan.year),
    })
    with locked_accounts(keys):
        _restart_read_snapshot(db)
        loan = _get_loan(db, loan_id)
        if loan.status == ACTIVE:
            db.refresh(loan)
            return loan
        if loan.status != RESERVED:
            raise BorrowingError("借据未处于已冻结状态，不能拨付")

        amount = _round4(loan.amount)
        tx_date = _today()
        try:
            with transactional(db):
                if _transit_loan(db, loan_id, (RESERVED,), ACTIVE) != 1:
                    raise BorrowingError("借据状态已变化，拨付失败，请刷新后重试")

                lender_acc = lock_row_for_write(db, lender_acc.id)
                borrower_acc = lock_row_for_write(db, borrower_acc.id)
                lender_name = _company_name(db, loan.lender_id)
                borrower_name = _company_name(db, loan.borrower_id)

                # 出借方：冻结配额出库（持仓与占用同减，frozen 不变）
                s_bal, s_frz, s_rsv = apply_ledger_delta(
                    db, lender_acc.id, -amount, 0, -amount
                )
                _add_ledger(
                    db, lender_acc, "loan_disburse_out", amount,
                    s_bal, s_frz, s_rsv, borrower_name,
                    f"借据 {loan.loan_no} 拨付，向{borrower_name}划出 {amount} 吨",
                    loan, tx_date,
                )
                # 借入方：配额到账
                b_bal, b_frz, b_rsv = apply_ledger_delta(db, borrower_acc.id, amount, 0, 0)
                _add_ledger(
                    db, borrower_acc, "loan_disburse_in", amount,
                    b_bal, b_frz, b_rsv, lender_name,
                    f"借据 {loan.loan_no} 拨付，从{lender_name}借入到账 {amount} 吨",
                    loan, tx_date,
                )

                # 年度配额闭环：到账配额同事务核销借入方同年度履约缺口
                try:
                    settle_borrower_deficit_on_loan(
                        db, loan, tx_date,
                        auto_clear=bool(int(loan.auto_clear_deficit)),
                    )
                except ValueError as exc:
                    raise BorrowingError(f"拨付联动履约核销失败，整笔拨付已回滚：{exc}")

                loan = _get_loan(db, loan_id)
                loan.disbursed_by = operator.id if operator else None
                loan.disbursed_at = datetime.utcnow()
                write_audit(
                    db, operator, "loan.disburse",
                    target_id=loan_id, loan_id=loan_id,
                    detail=f"监管拨付借据 {loan.loan_no}：{lender_name} → {borrower_name} "
                    f"{amount:g} 吨"
                    + ("，并联动核销借入方履约缺口"
                       if int(loan.auto_clear_deficit) else ""),
                )
                db.flush()
                db.refresh(loan)
        except InsufficientBalanceError:
            raise BorrowingError("借据拨付划转失败，账本状态异常，拨付已整体回滚")
        return loan


# --------------------------------------------------------------------------- #
# 到期标记
# --------------------------------------------------------------------------- #

def mark_overdue(db: Session, year: int, *, operator: Operator | None = None) -> list[QuotaLoan]:
    """监管批量标记：已过约定归还日、未结清且仍为 active 的借据置为 overdue。

    纯状态收敛操作，重复执行幂等；只动借据状态与审计，不动账。
    """
    today = _today()
    loans = (
        db.query(QuotaLoan)
        .filter(
            QuotaLoan.year == year,
            QuotaLoan.status == ACTIVE,
            QuotaLoan.due_date < today,
        )
        .order_by(QuotaLoan.id.asc())
        .all()
    )
    marked: list[QuotaLoan] = []
    for loan_ref in loans:
        with locked_accounts([quota_loan_key(loan_ref.id)]):
            _restart_read_snapshot(db)
            with transactional(db):
                result = db.execute(
                    update(QuotaLoan)
                    .where(QuotaLoan.id == loan_ref.id, QuotaLoan.status == ACTIVE)
                    .values(status=OVERDUE, overdue_at=datetime.utcnow())
                    .execution_options(synchronize_session=False)
                )
                if result.rowcount == 1:
                    loan = _get_loan(db, loan_ref.id)
                    marked.append(loan)
                    write_audit(
                        db, operator, "loan.overdue",
                        target_id=loan.id, loan_id=loan.id,
                        detail=f"借据 {loan.loan_no} 已超过约定归还日 {loan.due_date}，标记逾期",
                    )
                    db.flush()
    return marked


# --------------------------------------------------------------------------- #
# 归还（借入方主动，可分次）
# --------------------------------------------------------------------------- #

def _apply_transfer_repayment(
    db: Session,
    loan: QuotaLoan,
    amount: float,
    tx_date: str,
    *,
    out_type: str,
    in_type: str,
    source: str,
    operator: Operator | None,
    remark: str,
    idempotency_key: str | None,
) -> QuotaLoanRepayment:
    """在已持锁的写事务内执行一笔借入方 → 出借方配额划转并登记归还/追偿明细。"""
    borrower_acc = lock_row_for_write(db, _get_account(db, loan.borrower_id, loan.year).id)
    lender_acc = lock_row_for_write(db, _get_account(db, loan.lender_id, loan.year).id)
    lender_name = _company_name(db, loan.lender_id)
    borrower_name = _company_name(db, loan.borrower_id)

    b_bal, b_frz, b_rsv = apply_ledger_delta(db, borrower_acc.id, -amount, 0, 0)
    _add_ledger(
        db, borrower_acc, out_type, amount,
        b_bal, b_frz, b_rsv, lender_name,
        f"{remark}（借据 {loan.loan_no}，划出 {amount:g} 吨）",
        loan, tx_date,
    )
    l_bal, l_frz, l_rsv = apply_ledger_delta(db, lender_acc.id, amount, 0, 0)
    _add_ledger(
        db, lender_acc, in_type, amount,
        l_bal, l_frz, l_rsv, borrower_name,
        f"{remark}（借据 {loan.loan_no}，到账 {amount:g} 吨）",
        loan, tx_date,
    )

    repayment = QuotaLoanRepayment(
        repay_no=_gen_repay_no(db),
        loan_id=loan.id,
        year=loan.year,
        lender_id=loan.lender_id,
        borrower_id=loan.borrower_id,
        quantity=amount,
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
    company_id: int,
    *,
    amount: float | None = None,
    idempotency_key: str | None = None,
    operator: Operator | None = None,
) -> QuotaLoanRepayment:
    """借入方归还借据（active/overdue），可分次；amount 缺省归还全部未偿本金。

    - 仅借入方企业可发起；
    - 只从自由可用（current − frozen − reserved）划出，绝不挪用履约/交易占用；
    - 足额归还后借据 → repaid；重复请求由幂等键去重。
    """
    if amount is not None and amount <= 0:
        raise BorrowingError("归还数量必须为正数")
    loan = _get_loan(db, loan_id)
    if company_id != loan.borrower_id:
        raise BorrowingError("仅借入方企业可归还借据")
    if loan.status not in (ACTIVE, OVERDUE):
        raise BorrowingError(f"借据当前为 {loan.status}，不能主动归还")

    borrower_acc = _get_account(db, loan.borrower_id, loan.year)
    lender_acc = _get_account(db, loan.lender_id, loan.year)
    keys = sorted({
        quota_loan_key(loan_id),
        account_lock_key(borrower_acc.id),
        account_lock_key(lender_acc.id),
        company_clear_key(loan.borrower_id, loan.year),
        company_clear_key(loan.lender_id, loan.year),
    })
    with locked_accounts(keys):
        _restart_read_snapshot(db)
        loan = _get_loan(db, loan_id)
        if loan.status not in (ACTIVE, OVERDUE):
            raise BorrowingError(f"借据当前为 {loan.status}，不能主动归还")
        if idempotency_key:
            existing = (
                db.query(QuotaLoanRepayment)
                .filter(QuotaLoanRepayment.idempotency_key == idempotency_key)
                .first()
            )
            if existing:
                return existing

        outstanding = _outstanding_principal(loan)
        wanted = outstanding if amount is None else _round4(amount)
        pay = round(min(wanted, outstanding), 4)
        if pay <= 0:
            raise BorrowingError("借据已无待归还本金")

        try:
            with transactional(db):
                borrower_acc = lock_row_for_write(db, borrower_acc.id)
                free = _free_balance(borrower_acc)
                pay = round(min(pay, max(free, 0.0)), 4)
                if pay <= 0:
                    raise InsufficientBalanceError("借入方自由可用不足")

                repayment = _apply_transfer_repayment(
                    db, loan, pay, _today(),
                    out_type="loan_repay_out", in_type="loan_repay_in",
                    source=SRC_REPAY, operator=operator,
                    remark="借入方到期归还配额", idempotency_key=idempotency_key,
                )

                # 条件 UPDATE 兜底：并发下累计归还不超过本金
                loan = _bump_loan_returned(db, loan_id, pay)
                fully = loan.status == REPAID
                write_audit(
                    db, operator, "loan.repay",
                    target_id=loan_id, loan_id=loan_id,
                    detail=f"借入方 {_company_name(db, loan.borrower_id)} 归还借据 "
                    f"{loan.loan_no} {pay:g} 吨"
                    + ("，借据已结清" if fully else f"，剩余本金 {_outstanding_principal(loan):g} 吨"),
                )
                db.flush()
                db.refresh(repayment)
        except InsufficientBalanceError:
            raise BorrowingError("借入方自由可用配额不足或借据已被并发清偿，归还失败，请刷新后重试")
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
# 违约：监管宣告（尽力收回）与追偿（手动 / 自动）
# --------------------------------------------------------------------------- #

def declare_default(
    db: Session,
    loan_id: int,
    operator: Operator | None,
    reason: str,
) -> dict:
    """监管对逾期（overdue）借据宣告违约：尽力收回自由可用，不足登记违约欠额。

    - 仅 overdue 借据可宣告违约（先由监管批量标记逾期，再走违约追偿程序）；
    - 收回部分即时划付出借方（loan_default_clawback_out/in）；
    - 不足部分登记 defaulted_amount（表外债权，不豁免借入方义务）；
      若借入方自由可用足以覆盖全部未偿本金，则按足额归还处理，借据直接 repaid。
    """
    reason = (reason or "").strip()
    if len(reason) < 2:
        raise BorrowingError("请填写违约原因（至少 2 个字符）")
    loan = _get_loan(db, loan_id)
    if loan.status == DEFAULTED:
        raise BorrowingError("借据已宣告违约")
    if loan.status != OVERDUE:
        raise BorrowingError(f"借据当前为 {loan.status}，仅逾期借据可宣告违约")

    borrower_acc = _get_account(db, loan.borrower_id, loan.year)
    lender_acc = _get_account(db, loan.lender_id, loan.year)
    keys = sorted({
        quota_loan_key(loan_id),
        account_lock_key(borrower_acc.id),
        account_lock_key(lender_acc.id),
        company_clear_key(loan.borrower_id, loan.year),
        company_clear_key(loan.lender_id, loan.year),
    })
    with locked_accounts(keys):
        _restart_read_snapshot(db)
        loan = _get_loan(db, loan_id)
        if loan.status != OVERDUE:
            raise BorrowingError(f"借据当前为 {loan.status}，不能宣告违约")

        outstanding = _outstanding_principal(loan)
        try:
            with transactional(db):
                if _transit_loan(db, loan_id, (OVERDUE,), DEFAULTED) != 1:
                    raise BorrowingError("借据状态已变化，宣告违约失败，请刷新后重试")

                borrower_acc = lock_row_for_write(db, borrower_acc.id)
                recovered = round(min(outstanding, max(_free_balance(borrower_acc), 0.0)), 4)
                defaulted = round(outstanding - recovered, 4)

                if recovered > 0:
                    _apply_transfer_repayment(
                        db, loan, recovered, _today(),
                        out_type="loan_default_clawback_out",
                        in_type="loan_default_clawback_in",
                        source=SRC_CLAWBACK, operator=operator,
                        remark=f"监管宣告违约，尽力收回配额（{reason}）",
                        idempotency_key=None,
                    )

                # 条件 UPDATE 累计归还（兜底不超本金），随后按重读结果决定终态
                loan = _bump_loan_returned(
                    db, loan_id, recovered, new_status_if_complete=None
                )
                loan.defaulted_amount = defaulted
                loan.recovered_default = 0.0
                loan.defaulted_by = operator.id if operator else None
                loan.defaulted_at = datetime.utcnow()
                if defaulted <= 1e-9:
                    # 自由可用足额覆盖：收回即结清，借据不留在 defaulted
                    loan.status = REPAID
                    loan.closed_at = datetime.utcnow()

                write_audit(
                    db, operator, "loan.default",
                    target_id=loan_id, loan_id=loan_id,
                    detail=f"监管宣告借据 {loan.loan_no} 违约：应收回 {outstanding:g} 吨，"
                    f"实际收回 {recovered:g} 吨，登记欠额 {defaulted:g} 吨；原因：{reason}",
                )
                db.flush()
                result = {
                    "recovered": recovered,
                    "defaulted": defaulted,
                    "status": loan.status,
                }
        except InsufficientBalanceError:
            raise BorrowingError("违约收回划转失败，账本状态异常，操作已回滚")
        return result


def _apply_one_recovery(
    db: Session,
    loan: QuotaLoan,
    amount: float,
    tx_date: str,
    *,
    source: str,
    operator: Operator | None,
    remark: str,
    idempotency_key: str | None = None,
) -> QuotaLoanRepayment:
    """在已持锁的写事务内执行一笔违约欠额追偿（借入方出库 → 出借方到账）。"""
    return _apply_transfer_repayment(
        db, loan, amount, tx_date,
        out_type="loan_default_recover_out",
        in_type="loan_default_recover_in",
        source=source, operator=operator, remark=remark,
        idempotency_key=idempotency_key,
    )


def recover_borrower_loan_arrears(
    db: Session,
    borrower_id: int,
    year: int,
    tx_date: str | None = None,
    *,
    source: str = SRC_AUTO,
    auto_only: bool = True,
    operator: Operator | None = None,
    remark: str = "",
) -> dict:
    """用借入方自由可用配额追偿其该年度全部借据违约欠额。

    自动路径（订单交割/竞价结算到账后，``auto_only=True``）：只追偿借据本身
    开启 ``auto_recover_default`` 的欠额；借入方可通过建单时关闭该开关选择
    “到账不自动划扣、事后由监管手动追偿”。手动入口（监管）传
    ``auto_only=False``，不受借据开关限制。

    自动路径不自行开启事务、不自行提交，与触发它的业务同生共死；
    调用方的外层锁集合已含借入方 account/clear 键。本函数按全局锁序
    （account < auction < clear < loan < order）补齐涉及的借据键与各出借方
    账户键——全部按名排序获取，嵌套调用不会形成锁环。
    手动入口（监管）请用 :func:`recover_loan_defaults`，那里负责事务边界。

    先清缴后追偿：仅使用 current − frozen − reserved；逐笔按借据 id 顺序偿还。
    返回 ``{"repayments": [...], "recovered": x}``；无欠额/无可用为空操作。
    """
    loans = list_defaulted_loans(db, borrower_id=borrower_id, year=year,
                                 auto_only=auto_only)
    if not loans:
        return {"repayments": [], "recovered": 0.0}

    # 收集借据键 + 借入方与全部受偿出借方的账户/清缴键（按名排序统一获取）
    keys = {
        quota_loan_key(loan.id) for loan in loans
    }
    company_ids = {borrower_id}
    for loan in loans:
        company_ids.add(loan.lender_id)
    for cid in company_ids:
        acc = _get_account(db, cid, year)
        keys.add(account_lock_key(acc.id))
        keys.add(company_clear_key(cid, year))

    with locked_accounts(sorted(keys)):
        # 取锁后重查，过滤并发下已结清的借据
        loans = [
            db.get(QuotaLoan, loan.id)
            for loan in loans
        ]
        loans = [loan for loan in loans if loan and loan.status == DEFAULTED
                 and _outstanding_default(loan) > 1e-9]
        if not loans:
            return {"repayments": [], "recovered": 0.0}

        borrower_acc = lock_row_for_write(db, _get_account(db, borrower_id, year).id)
        budget = max(_free_balance(borrower_acc), 0.0)
        repayments: list[QuotaLoanRepayment] = []
        recovered_total = 0.0

        for loan in loans:
            if budget <= 1e-9:
                break
            outstanding = _outstanding_default(loan)
            pay = round(min(outstanding, budget), 4)
            if pay <= 0:
                continue
            lock_row_for_write(db, _get_account(db, loan.lender_id, year).id)
            rmk = remark or (
                "借入方配额到账自动追偿违约欠额" if source == SRC_AUTO else "监管手动追偿违约欠额"
            )
            repayment = _apply_one_recovery(
                db, loan, pay, tx_date or _today(),
                source=source, operator=operator, remark=rmk,
            )
            new_recovered = round(_num(loan.recovered_default) + pay, 4)
            # 条件 UPDATE 兜底：追偿不超过欠额登记、累计归还不超过本金
            recovered_expr = QuotaLoan.recovered_default + pay
            returned_expr = QuotaLoan.returned_amount + pay
            upd = db.execute(
                update(QuotaLoan)
                .where(QuotaLoan.id == loan.id)
                .where(recovered_expr <= QuotaLoan.defaulted_amount)
                .where(returned_expr <= QuotaLoan.amount)
                .values(recovered_default=recovered_expr, returned_amount=returned_expr)
                .execution_options(synchronize_session=False)
            )
            if upd.rowcount != 1:
                raise InsufficientBalanceError("借据欠额已被并发请求追偿结清")
            db.expire_all()
            loan = db.get(QuotaLoan, loan.id)
            fully = (
                _num(loan.recovered_default) + 1e-9 >= _num(loan.defaulted_amount)
                and _num(loan.returned_amount) + 1e-9 >= _num(loan.amount)
            )
            if fully:
                loan.status = REPAID
                loan.closed_at = datetime.utcnow()
            write_audit(
                db, operator,
                "loan.default.auto_recover" if source == SRC_AUTO else "loan.default.recover",
                target_id=loan.id, loan_id=loan.id,
                detail=f"{'自动' if source == SRC_AUTO else '手动'}追偿借据 {loan.loan_no} "
                f"{pay:g} 吨" + ("，借据欠额已结清" if fully else ""),
            )
            repayments.append(repayment)
            budget = round(budget - pay, 4)
            recovered_total = round(recovered_total + pay, 4)

        db.flush()
        return {"repayments": repayments, "recovered": recovered_total}


def recover_loan_defaults(
    db: Session,
    borrower_id: int,
    year: int,
    operator: Operator | None,
) -> dict:
    """监管手动入口：追偿借入方某年度全部借据违约欠额（自由可用不足则尽力而为）。

    手动追偿不受借据 ``auto_recover_default`` 开关限制。
    """
    loans = list_defaulted_loans(db, borrower_id=borrower_id, year=year)
    if not loans:
        raise BorrowingError("该企业该年度没有待追偿的借据违约欠额")

    borrower_acc = _get_account(db, borrower_id, year)
    keys = {account_lock_key(borrower_acc.id), company_clear_key(borrower_id, year)}
    for loan in loans:
        keys.add(quota_loan_key(loan.id))
        lender_acc = _get_account(db, loan.lender_id, year)
        keys.add(account_lock_key(lender_acc.id))
        keys.add(company_clear_key(loan.lender_id, year))

    with locked_accounts(sorted(keys)):
        try:
            with transactional(db):
                result = recover_borrower_loan_arrears(
                    db, borrower_id, year, _today(),
                    source=SRC_MANUAL, auto_only=False, operator=operator,
                    remark="监管手动触发借据违约追偿",
                )
                db.flush()
                for r in result["repayments"]:
                    db.refresh(r)
        except InsufficientBalanceError:
            raise BorrowingError("借据违约追偿划转失败，账本状态异常，操作已回滚")
        if not result["repayments"]:
            raise BorrowingError("借入方自由可用配额不足，暂无可追偿配额，请待其补足后重试")
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
    party_role: str | None = None,
) -> list[QuotaLoan]:
    """借据列表。

    - company_id 给定且 party_role=None：该企业作为出借方或借入方参与的借据；
    - party_role='lender'/'borrower'：仅该角色侧借据；
    - 监管侧不给 company_id：全部借据。
    """
    q = db.query(QuotaLoan)
    if company_id is not None:
        if party_role == "lender":
            q = q.filter(QuotaLoan.lender_id == company_id)
        elif party_role == "borrower":
            q = q.filter(QuotaLoan.borrower_id == company_id)
        else:
            q = q.filter(
                (QuotaLoan.lender_id == company_id) | (QuotaLoan.borrower_id == company_id)
            )
    if year is not None:
        q = q.filter(QuotaLoan.year == year)
    if status:
        q = q.filter(QuotaLoan.status == status)
    return q.order_by(QuotaLoan.id.desc()).all()


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


def list_defaulted_loans(
    db: Session,
    *,
    borrower_id: int | None = None,
    year: int | None = None,
    auto_only: bool = False,
) -> list[QuotaLoan]:
    """存在未追偿违约欠额的借据（供监管查询与自动追偿扫描）。

    ``auto_only=True`` 时仅返回开启 ``auto_recover_default`` 的借据
    （订单交割/竞价结算的自动追偿路径使用）；手动追偿与监管查询不过滤。
    """
    q = db.query(QuotaLoan).filter(
        QuotaLoan.defaulted_amount > QuotaLoan.recovered_default
    )
    if auto_only:
        q = q.filter(QuotaLoan.auto_recover_default == 1)
    if borrower_id is not None:
        q = q.filter(QuotaLoan.borrower_id == borrower_id)
    if year is not None:
        q = q.filter(QuotaLoan.year == year)
    return q.order_by(QuotaLoan.id.asc()).all()


def list_due_loans(db: Session, year: int) -> list[QuotaLoan]:
    """已过约定归还日、仍未结清（active）的借据（逾期标记候选）。"""
    today = _today()
    return (
        db.query(QuotaLoan)
        .filter(
            QuotaLoan.year == year,
            QuotaLoan.status == ACTIVE,
            QuotaLoan.due_date < today,
        )
        .order_by(QuotaLoan.id.asc())
        .all()
    )


def list_audit_logs(db: Session, *, loan_id: int | None = None, limit: int = 200) -> list[QuotaLoanAuditLog]:
    q = db.query(QuotaLoanAuditLog)
    if loan_id is not None:
        q = q.filter(QuotaLoanAuditLog.loan_id == loan_id)
    return q.order_by(QuotaLoanAuditLog.id.desc()).limit(limit).all()
