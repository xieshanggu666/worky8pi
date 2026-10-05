"""配额管理：年度配额分配、报告批准冻结、履约清缴与冲正。

跨模块闭环：
- 配额分配写入年度配额、账户和入账流水；
- MRV 报告批准后以报告快照排放量创建/更新履约记录，并冻结可用配额；
- 清缴优先核销已冻结配额，不足部分再扣减可用配额，买入配额后可补缴缺口；
- 报告冲正会解冻冻结配额、退还已清缴配额并归档旧履约记录；其中竞价成交单
  归属的到账补缴按成交单逐笔退还（auction_clear_refund 关联成交单），与
  竞价成交冲正共用同一本成交单归属流水账，两条回退链路顺序无关、不重复退还；
- 余额、冻结额、流水、配额状态、履约记录和报告状态在同一事务提交，
  任一步失败均整体回滚。
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import func, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.ledger import (
    InsufficientBalanceError,
    account_lock_key,
    apply_ledger_delta,
    atomic_available_debit,
    company_clear_key,
    is_duplicate_submit,
    lock_row_for_write,
    locked_accounts,
    transactional,
)
from app.models.allowance import (
    AllowanceAccount,
    AllowanceTransaction,
    ComplianceRecord,
    Quota,
    TradeOrder,
)
from app.models.auction import AuctionTrade
from app.models.company import Company
from app.models.report import MrvReport
from app.services.calculation_service import annual_total, count_unverified_activities

if TYPE_CHECKING:
    from app.models.loan import QuotaLoan

def _today() -> str:
    return datetime.utcnow().strftime("%Y-%m-%d")


def _set_quota_status(db: Session, company_id: int, year: int, status: str) -> None:
    quota = (
        db.query(Quota)
        .filter(Quota.company_id == company_id, Quota.year == year)
        .first()
    )
    if quota:
        quota.status = status


def _add_ledger_tx(
    db: Session,
    account: AllowanceAccount,
    tx_type: str,
    amount: float,
    balance_after: float,
    frozen_after: float,
    counterparty: str,
    remark: str,
    tx_date: str | None = None,
    idempotency_key: str | None = None,
    reserved_after: float | None = None,
    trade_order_id: int | None = None,
    auction_trade_id: int | None = None,
    loan_id: int | None = None,
    price: float | None = None,
) -> AllowanceTransaction:
    tx = AllowanceTransaction(
        account_id=account.id,
        company_id=account.company_id,
        tx_type=tx_type,
        amount=round(amount, 4),
        counterparty=counterparty,
        price=round(price, 2) if price is not None else None,
        tx_date=tx_date or _today(),
        balance_after=round(balance_after, 4),
        frozen_after=round(frozen_after, 4),
        reserved_after=round(reserved_after if reserved_after is not None else account.reserved_balance, 4),
        trade_order_id=trade_order_id,
        auction_trade_id=auction_trade_id,
        loan_id=loan_id,
        remark=remark,
        idempotency_key=idempotency_key,
    )
    db.add(tx)
    return tx


def allocate_quota(
    db: Session,
    company_id: int,
    year: int,
    baseline: float,
    allocation_amount: float,
    adjustment: float = 0.0,
) -> Quota:
    """免费配额分配：写入配额、初始化账户、登记划入流水。

    同一企业同一年度重复分配返回已有配额，不重复入账；并发提交由唯一约束、
    企业年度键和账户键共同保证只有一笔生效。
    """
    account = (
        db.query(AllowanceAccount)
        .filter(AllowanceAccount.company_id == company_id, AllowanceAccount.year == year)
        .first()
    )
    keys = [company_clear_key(company_id, year)]
    if account:
        keys.append(account_lock_key(account.id))

    with locked_accounts(keys):
        existing = (
            db.query(Quota)
            .filter(Quota.company_id == company_id, Quota.year == year)
            .first()
        )
        if existing:
            return existing

        total = round(allocation_amount + adjustment, 4)
        if total < 0:
            raise ValueError("配额分配净额不能为负数")

        try:
            with transactional(db):
                quota = Quota(
                    company_id=company_id,
                    year=year,
                    baseline=round(baseline, 4),
                    allocation_amount=round(allocation_amount, 4),
                    adjustment=round(adjustment, 4),
                    total=total,
                    status="allocated",
                    allocated_at=datetime.utcnow(),
                )
                db.add(quota)

                if account:
                    # 已存在账户：原子加记配额与期初值
                    account = lock_row_for_write(db, account.id)
                    balance_after, frozen_after, reserved_after = apply_ledger_delta(db, account.id, total, 0, 0)
                    db.execute(
                        update(AllowanceAccount)
                        .where(AllowanceAccount.id == account.id)
                        .values(opening_balance=AllowanceAccount.opening_balance + total)
                        .execution_options(synchronize_session=False)
                    )
                    db.flush()
                else:
                    account = AllowanceAccount(
                        company_id=company_id,
                        year=year,
                        opening_balance=total,
                        current_balance=total,
                        frozen_balance=0,
                    )
                    db.add(account)
                    db.flush()
                    balance_after, frozen_after = total, 0.0

                _add_ledger_tx(
                    db,
                    account,
                    "allocation",
                    total,
                    balance_after,
                    frozen_after,
                    "主管部门",
                    f"{year}年度免费配额分配",
                )
                db.flush()
                db.refresh(quota)
        except IntegrityError as exc:
            # 并发分配竞态：另一请求已插入同年配额，回滚后返回已有记录
            if is_duplicate_submit(exc, "uq_quota_company_year"):
                db.rollback()
                return (
                    db.query(Quota)
                    .filter(Quota.company_id == company_id, Quota.year == year)
                    .one()
                )
            raise
        return quota


def _get_account(db: Session, company_id: int, year: int) -> AllowanceAccount | None:
    return (
        db.query(AllowanceAccount)
        .filter(AllowanceAccount.company_id == company_id, AllowanceAccount.year == year)
        .first()
    )


def _get_active_record(db: Session, company_id: int, year: int) -> ComplianceRecord | None:
    return (
        db.query(ComplianceRecord)
        .filter(
            ComplianceRecord.company_id == company_id,
            ComplianceRecord.year == year,
            ComplianceRecord.is_active == 1,
        )
        .first()
    )


def freeze_allowance_for_report(
    db: Session,
    report: MrvReport,
    verifier_id: int,
) -> ComplianceRecord:
    """批准报告时按报告排放快照冻结配额，并建立活跃履约记录。"""
    if report.status != "submitted":
        raise ValueError("仅已提交的报告可批准")

    company_id = report.company_id
    year = report.year

    # 数据状态约束：存在未核验活动数据时禁止批准，防止未核查数据经报告快照
    # 冻结配额、形成履约结果，污染年度配额闭环；核验并重新核算、重新生成报告后方可批准。
    pending = count_unverified_activities(db, company_id, year)
    if pending:
        raise ValueError(
            f"该企业{year}年度仍有 {pending} 条活动数据未核验，不能批准报告；"
            "请先完成核验、重新核算并重新生成报告"
        )

    # 快照一致性拦截：批量核验/重算可能已使核算结果领先于报告快照。
    # 若报告排放快照与最新核算合计不一致，说明报告过期，必须重新生成后再提交批准，
    # 防止用旧快照冻结配额、形成错误的履约义务。
    latest_total = annual_total(db, company_id, year)
    if abs(latest_total - round(float(report.total_emission), 4)) > 0.01:
        raise ValueError(
            f"报告排放快照（{float(report.total_emission):.4f}）与最新核算结果"
            f"（{latest_total:.4f}）不一致，数据核验后已重算；请重新生成并提交报告后再批准"
        )

    emission = round(float(report.total_emission), 4)
    account = _get_account(db, company_id, year)
    keys = [company_clear_key(company_id, year)]
    if account:
        keys.append(account_lock_key(account.id))

    with locked_accounts(keys):
        existing = _get_active_record(db, company_id, year)
        if existing and existing.report_id not in (None, report.id):
            raise ValueError("该企业年度已有生效履约记录，请先冲正原批准报告")

        try:
            with transactional(db):
                if existing is None:
                    record = ComplianceRecord(
                        company_id=company_id,
                        year=year,
                        verified_emission=emission,
                        cleared_amount=0,
                        frozen_amount=0,
                        deficit=emission,
                        status="pending",
                        deadline=f"{year}-12-31",
                        report_id=report.id,
                        is_active=1,
                    )
                    db.add(record)
                    db.flush()
                    already_cleared = 0.0
                    already_frozen = 0.0
                else:
                    # 兼容历史手动清缴：批准报告时将原记录绑定到报告，并按剩余义务补冻结。
                    record = existing
                    already_cleared = round(float(record.cleared_amount), 4)
                    already_frozen = round(float(record.frozen_amount), 4)
                    if already_cleared > emission:
                        raise ValueError("已清缴量大于批准排放量，请先冲正历史清缴后再批准")
                    record.verified_emission = emission
                    record.report_id = report.id
                    record.deadline = record.deadline or f"{year}-12-31"

                remaining_obligation = round(emission - already_cleared, 4)

                frozen = 0.0
                balance_after = frozen_after = reserved_after = 0.0
                if account and remaining_obligation > 0:
                    account = lock_row_for_write(db, account.id)
                    # 履约冻结只能使用真正未占用的配额：已被确认中订单占用的
                    # 交易配额（reserved）不得再被冻结，反之亦然。
                    available = round(
                        float(account.current_balance)
                        - float(account.frozen_balance)
                        - float(account.reserved_balance),
                        4,
                    )
                    to_freeze = round(max(remaining_obligation - already_frozen, 0.0), 4)
                    frozen = round(min(max(available, 0.0), to_freeze), 4)
                    if frozen > 0:
                        balance_after, frozen_after, reserved_after = apply_ledger_delta(
                            db, account.id, 0, frozen, 0
                        )
                        _add_ledger_tx(
                            db,
                            account,
                            "freeze",
                            frozen,
                            balance_after,
                            frozen_after,
                            "MRV批准冻结",
                            f"{year}年度报告批准，冻结履约配额 {frozen} 吨",
                            reserved_after=reserved_after,
                        )
                    else:
                        balance_after = float(account.current_balance)
                        frozen_after = float(account.frozen_balance)
                        reserved_after = float(account.reserved_balance)

                total_frozen = round(already_frozen + frozen, 4)
                record.frozen_amount = total_frozen
                record.deficit = round(emission - already_cleared - total_frozen, 4)
                if emission <= 0 or already_cleared >= emission:
                    record.status = "compliant"
                else:
                    record.status = "pending" if record.deficit <= 0 else "deficit"

                if emission <= 0 or already_cleared >= emission:
                    quota_status = "cleared"
                else:
                    quota_status = "frozen" if record.deficit <= 0 else "allocated"
                _set_quota_status(db, company_id, year, quota_status)

                report.status = "approved"
                report.approved_by = verifier_id
                report.approved_at = datetime.utcnow()

                db.flush()
                db.refresh(record)
                db.refresh(report)
                if account:
                    db.refresh(account)
        except InsufficientBalanceError:
            raise ValueError("可用配额不足，报告批准冻结失败")
        except IntegrityError as exc:
            # 并发批准同一企业年度报告：首个事务提交后，后到事务由部分唯一索引拒绝。
            # SQLite 的报错可能只列出列名，不显示索引名，因此还需按企业+年度重查。
            message = str(getattr(exc, "orig", exc))
            looks_like_active_duplicate = (
                "uq_compliance_active_company_year" in message
                or (
                    "UNIQUE constraint failed" in message
                    and "compliance_records.company_id" in message
                    and "compliance_records.year" in message
                )
            )
            if looks_like_active_duplicate:
                db.rollback()
                existing = _get_active_record(db, company_id, year)
                if existing:
                    db.refresh(report)
                    return existing
            raise
        return record


def _unrefunded_auction_clearance(db: Session, account_id: int) -> list[tuple[int, float]]:
    """按成交单汇总账户上“竞价到账补缴”的未退还净额，返回 ``[(成交单id, 净额), …]``。

    竞价结算联动补缴（``auction_deficit_clear``）与两条回退链路的退还
    （``auction_clear_refund``：报告冲正与成交冲正都会写）均按成交单归属记账，
    二者差额即仍可退还的补缴量。报告冲正与成交冲正共用同一本成交单归属
    流水账：任意先后顺序下同一吨补缴最多退还一次，系统总配额守恒。
    """
    rows = (
        db.query(
            AllowanceTransaction.auction_trade_id,
            AllowanceTransaction.tx_type,
            func.coalesce(func.sum(AllowanceTransaction.amount), 0),
        )
        .filter(
            AllowanceTransaction.account_id == account_id,
            AllowanceTransaction.auction_trade_id.isnot(None),
            AllowanceTransaction.tx_type.in_(("auction_deficit_clear", "auction_clear_refund")),
        )
        .group_by(AllowanceTransaction.auction_trade_id, AllowanceTransaction.tx_type)
        .all()
    )
    nets: dict[int, float] = {}
    for trade_id, tx_type, total in rows:
        signed = float(total or 0.0) * (1 if tx_type == "auction_deficit_clear" else -1)
        nets[int(trade_id)] = round(nets.get(int(trade_id), 0.0) + signed, 4)
    return [(tid, amt) for tid, amt in sorted(nets.items()) if amt > 1e-9]


def reverse_approved_report(
    db: Session,
    report: MrvReport,
    operator_id: int,
    reason: str,
) -> ComplianceRecord:
    """冲正已批准报告：归档履约记录，解冻并退还已占用/清缴的配额。"""
    reason = (reason or "").strip()
    if report.status != "approved":
        raise ValueError("仅已批准的报告可冲正")
    if len(reason) < 2:
        raise ValueError("请填写冲正原因")

    company_id = report.company_id
    year = report.year
    account = _get_account(db, company_id, year)
    keys = [company_clear_key(company_id, year)]
    if account:
        keys.append(account_lock_key(account.id))

    with locked_accounts(keys):
        record = (
            db.query(ComplianceRecord)
            .filter(
                ComplianceRecord.company_id == company_id,
                ComplianceRecord.year == year,
                ComplianceRecord.report_id == report.id,
                ComplianceRecord.is_active == 1,
            )
            .first()
        )
        if record is None:
            raise ValueError("未找到报告对应的生效履约记录，无法冲正")

        frozen = round(float(record.frozen_amount), 4)
        cleared = round(float(record.cleared_amount), 4)
        refund = round(frozen + cleared, 4)

        try:
            with transactional(db):
                balance_after = frozen_after = reserved_after = 0.0
                if refund > 0:
                    if account is None:
                        raise ValueError("配额账户缺失，无法安全退还配额，冲正已中止")
                    lock_row_for_write(db, account.id)
                    # 竞价成交单归属的到账补缴按成交单逐笔退还并关联成交单
                    # （auction_clear_refund）：监管事后冲正该成交单时，其回退链路
                    # 按同一本成交单归属流水账计算可退余额，两条回退链路顺序无关，
                    # 同一吨补缴不会被重复退还（否则系统总配额凭空增加）。
                    trade_refunds = _unrefunded_auction_clearance(db, account.id)
                    trade_refund_total = round(sum(amt for _, amt in trade_refunds), 4)
                    # 成交单归属补缴必然已计入 cleared；防御性封顶，退还总额不超过已清缴量
                    trade_refund_total = min(trade_refund_total, cleared)
                    cleared_refund = round(cleared - trade_refund_total, 4)

                    # 1) 解冻：冻结配额从未离开持仓，仅冻结额回落、持仓不变
                    # （若把冻结额也加进持仓，系统总配额会凭空增加）。
                    if frozen > 0:
                        balance_after, frozen_after, reserved_after = apply_ledger_delta(
                            db, account.id, 0, -frozen, 0
                        )
                        _add_ledger_tx(
                            db,
                            account,
                            "reversal_unfreeze",
                            frozen,
                            balance_after,
                            frozen_after,
                            "报告冲正",
                            f"{year}年度报告冲正：解除履约冻结 {frozen} 吨",
                            reserved_after=reserved_after,
                        )

                    # 2) 非成交单归属的已清缴部分曾离开持仓，重新入账。
                    # 交易占用（reserved）不受影响，退还的配额成为可交易的自由配额。
                    if cleared_refund > 0:
                        balance_after, frozen_after, reserved_after = apply_ledger_delta(
                            db, account.id, cleared_refund, 0, 0
                        )
                        _add_ledger_tx(
                            db,
                            account,
                            "reversal",
                            cleared_refund,
                            balance_after,
                            frozen_after,
                            "报告冲正",
                            f"{year}年度报告冲正：退还已清缴配额 {cleared_refund} 吨",
                            reserved_after=reserved_after,
                        )

                    # 3) 成交单归属的已清缴部分按成交单逐笔退还（关联成交单）。
                    budget = trade_refund_total
                    for trade_id, net in trade_refunds:
                        if budget <= 1e-9:
                            break
                        amount = round(min(net, budget), 4)
                        budget = round(budget - amount, 4)
                        trade = db.get(AuctionTrade, trade_id)
                        trade_no = trade.trade_no if trade else str(trade_id)
                        balance_after, frozen_after, reserved_after = apply_ledger_delta(
                            db, account.id, amount, 0, 0
                        )
                        _add_ledger_tx(
                            db,
                            account,
                            "auction_clear_refund",
                            amount,
                            balance_after,
                            frozen_after,
                            "报告冲正",
                            f"{year}年度报告冲正：退还成交单 {trade_no} 归属的到账补缴 {amount} 吨",
                            reserved_after=reserved_after,
                            auction_trade_id=trade_id,
                        )

                record.is_active = 0
                record.status = "reversed"
                record.frozen_amount = 0
                record.deficit = 0
                _set_quota_status(db, company_id, year, "allocated")

                report.status = "reversed"
                report.reversed_by = operator_id
                report.reversed_at = datetime.utcnow()
                report.reversal_reason = reason

                db.flush()
                db.refresh(record)
                db.refresh(report)
                if account:
                    db.refresh(account)
        except InsufficientBalanceError:
            raise ValueError("配额账本状态异常，冲正失败并已回滚")
        return record


def _find_existing_clear(
    db: Session, company_id: int, year: int, idempotency_key: str | None
) -> ComplianceRecord | None:
    if not idempotency_key:
        return None
    return (
        db.query(ComplianceRecord)
        .filter(
            ComplianceRecord.company_id == company_id,
            ComplianceRecord.year == year,
            ComplianceRecord.idempotency_key == idempotency_key,
            ComplianceRecord.is_active == 1,
        )
        .first()
    )


def _apply_clearance(
    db: Session,
    company_id: int,
    year: int,
    deadline: str,
    *,
    idempotency_key: str | None = None,
    trade_order: "TradeOrder | None" = None,
    auction_trade: "AuctionTrade | None" = None,
    quota_loan: "QuotaLoan | None" = None,
    create_if_absent: bool = False,
) -> ComplianceRecord | None:
    """清缴核销内核：在调用方已开启的写事务内执行（不自行提交/回滚/加锁）。

    核销顺序：优先核销已履约冻结配额（current/frozen 同减），不足部分再从
    自由可用配额补扣（只减 current，绝不挪用交易占用 reserved）。
    累计清缴不超过核查排放量；已达标时为空操作。

    ``trade_order`` 非空表示该次核销由企业间订单交割联动触发：补扣流水记为
    ``trade_deficit_clear`` 并关联订单，交割日期作为清缴日期，形成
    “买入到账 → 缺口补缴 → 达标”的年度配额闭环。

    ``auction_trade`` 非空表示由集中竞价结算联动触发：补扣流水记为
    ``auction_deficit_clear`` 并关联竞价成交单，同属年度配额闭环。

    ``quota_loan`` 非空表示由配额借贷放款联动触发：补扣流水记为
    ``loan_deficit_clear`` 并关联借贷单——借入配额到账即用于履约，
    到期仍须用自有自由可用配额归还。

    ``create_if_absent`` 为真时（手动清缴兼容历史流程），若尚无活跃履约记录，
    按年度核算排放量新建一条；联动核销仅在既有记录上执行，不隐式建记录。

    调用方必须持有 ``clear:<company>:<year>`` 与对应账户键，并已进入写事务。
    """
    record = _get_active_record(db, company_id, year)
    if record is None and not create_if_absent:
        return None
    if record is not None and record.status == "reversed":
        raise ValueError("该履约记录已冲正归档，不能继续清缴")

    # 已批准报告以批准时的排放快照为准，防止报告批准后台账变化改变履约义务；
    # 兼容尚未接入“批准即冻结”的历史手动清缴流程。
    emission = (
        round(float(record.verified_emission), 4)
        if record is not None and record.report_id
        else annual_total(db, company_id, year)
    )
    already_cleared = round(float(record.cleared_amount), 4) if record is not None else 0.0

    # 已有记录且已达标：幂等空操作（无记录的零排放场景仍向下补建合规记录）
    if record is not None and (emission <= 0 or already_cleared >= emission):
        record.status = "compliant"
        record.deficit = 0
        db.flush()
        return record

    remaining = round(emission - already_cleared, 4)
    account = _get_account(db, company_id, year)

    frozen_available = round(float(record.frozen_amount), 4) if record is not None else 0.0
    frozen_use = round(min(frozen_available, remaining), 4)
    current_use = 0.0
    balance_after = frozen_after = reserved_after = 0.0

    if account:
        # 进入写事务即抢占账户行写锁：随后读到的可用余额在提交前不会被
        # 并发卖出/订单交割改变，清缴“预算 → 扣减”不再有 TOCTOU 窗口。
        account = lock_row_for_write(db, account.id)
        balance_after = float(account.current_balance)
        frozen_after = float(account.frozen_balance)
        reserved_after = float(account.reserved_balance)

    if frozen_use > 0:
        if account is None:
            raise ValueError("冻结配额对应账户缺失，清缴已中止")
        balance_after, frozen_after, reserved_after = apply_ledger_delta(
            db, account.id, -frozen_use, -frozen_use, 0
        )
        if trade_order is not None:
            f_seller = db.get(Company, trade_order.seller_id)
            f_counterparty = f_seller.name if f_seller else f"企业{trade_order.seller_id}"
            f_remark = f"订单 {trade_order.order_no} 交割，冻结配额履约清缴 {frozen_use} 吨"
            f_trade_order_id = trade_order.id
            f_auction_trade_id = None
            f_price = float(trade_order.price)
        else:
            # 冻结核销使用的是买方自有冻结配额，并非交易/竞价到账量，因此即使
            # 由结算联动触发，流水也不关联成交单（冲正成交单时不回滚这部分——
            # 交易取消不影响企业用自有冻结配额履约这一事实）。
            f_counterparty = "履约清缴"
            f_remark = f"{year}年度冻结配额履约清缴 {frozen_use} 吨"
            f_trade_order_id = None
            f_auction_trade_id = None
            f_price = None
        _add_ledger_tx(
            db,
            account,
            "frozen_clear",
            frozen_use,
            balance_after,
            frozen_after,
            f_counterparty,
            f_remark,
            tx_date=deadline,
            reserved_after=reserved_after,
            trade_order_id=f_trade_order_id,
            auction_trade_id=f_auction_trade_id,
            price=f_price,
        )

    still_remaining = round(remaining - frozen_use, 4)
    current_use = 0.0
    if still_remaining > 0 and account:
        # 清缴补扣只能使用自由可用配额，已确认订单占用的交易配额不得被清缴挪用。
        # 实扣额由数据库在写锁内按 min(剩余缺口, 自由可用) 原子计算，
        # 无需“先读余额做预算”，从根本上消除与并发卖出之间的 TOCTOU 竞态。
        current_use, balance_after, frozen_after, reserved_after = atomic_available_debit(
            db, account.id, still_remaining
        )
        if current_use > 0:
            if trade_order is not None:
                seller_name = db.get(Company, trade_order.seller_id)
                counterparty = seller_name.name if seller_name else f"企业{trade_order.seller_id}"
                tx_type = "trade_deficit_clear"
                remark = (
                    f"订单 {trade_order.order_no} 交割到账配额自动补缴{year}年度缺口 "
                    f"{current_use} 吨"
                )
                trade_order_id = trade_order.id
                auction_trade_id = None
                f_loan_id = None
                price = float(trade_order.price)
            elif auction_trade is not None:
                seller_name = db.get(Company, auction_trade.seller_id)
                counterparty = seller_name.name if seller_name else f"企业{auction_trade.seller_id}"
                tx_type = "auction_deficit_clear"
                remark = (
                    f"集中竞价 {auction_trade.trade_no} 结算到账配额自动补缴{year}年度缺口 "
                    f"{current_use} 吨"
                )
                trade_order_id = None
                auction_trade_id = auction_trade.id
                f_loan_id = None
                price = float(auction_trade.price)
            elif quota_loan is not None:
                lender = db.get(Company, quota_loan.lender_id)
                counterparty = lender.name if lender else f"企业{quota_loan.lender_id}"
                tx_type = "loan_deficit_clear"
                remark = (
                    f"借贷 {quota_loan.loan_no} 放款到账配额自动补缴{year}年度缺口 "
                    f"{current_use} 吨"
                )
                trade_order_id = None
                auction_trade_id = None
                f_loan_id = quota_loan.id
                price = float(quota_loan.price or 0)
            else:
                counterparty = "履约清缴"
                tx_type = "clear"
                remark = f"{year}年度可用配额履约清缴 {current_use} 吨"
                trade_order_id = None
                auction_trade_id = None
                f_loan_id = None
                price = None
            _add_ledger_tx(
                db,
                account,
                tx_type,
                current_use,
                balance_after,
                frozen_after,
                counterparty,
                remark,
                tx_date=deadline,
                idempotency_key=None if (trade_order is not None or auction_trade is not None or quota_loan is not None) else idempotency_key,
                reserved_after=reserved_after,
                trade_order_id=trade_order_id,
                auction_trade_id=auction_trade_id,
                loan_id=f_loan_id,
                price=price,
            )

    deducted = round(frozen_use + current_use, 4)
    cleared = round(already_cleared + deducted, 4)
    deficit = round(emission - cleared, 4)

    if record is None:
        record = ComplianceRecord(
            company_id=company_id,
            year=year,
            verified_emission=emission,
            deadline=deadline,
            idempotency_key=idempotency_key,
            is_active=1,
        )
        db.add(record)
    elif idempotency_key and not record.idempotency_key:
        record.idempotency_key = idempotency_key
    if deadline:
        record.deadline = deadline

    record.verified_emission = emission
    record.cleared_amount = cleared
    record.frozen_amount = round(frozen_available - frozen_use, 4)
    record.deficit = deficit
    if emission <= 0:
        record.status = "compliant"
    else:
        record.status = "compliant" if deficit <= 0 else "deficit"
    record.cleared_at = datetime.utcnow()

    if emission <= 0 or deficit <= 0:
        _set_quota_status(db, company_id, year, "cleared")
    elif frozen_available or current_use:
        _set_quota_status(db, company_id, year, "allocated")

    db.flush()
    return record


def clear_emission(
    db: Session,
    company_id: int,
    year: int,
    deadline: str,
    idempotency_key: str | None = None,
) -> ComplianceRecord:
    """履约清缴/缺口补缴：优先核销冻结配额，再扣减可用配额。

    已达标重复调用为幂等空操作；deficit 状态可在买入或补充分配后再次调用，
    且累计清缴不会超过核查排放量。无账户或可用余额不足时只核销能够覆盖的部分。
    """
    account = _get_account(db, company_id, year)
    keys = [company_clear_key(company_id, year)]
    if account:
        keys.append(account_lock_key(account.id))

    with locked_accounts(keys):
        if idempotency_key:
            existing = _find_existing_clear(db, company_id, year, idempotency_key)
            if existing:
                return existing

        try:
            with transactional(db):
                record = _apply_clearance(
                    db,
                    company_id,
                    year,
                    deadline,
                    idempotency_key=idempotency_key,
                    create_if_absent=True,
                )
                if record is not None:
                    db.refresh(record)
                if account:
                    db.refresh(account)
        except InsufficientBalanceError:
            # 键锁之后仍被并发改动的极端情况：原子更新兜底拒绝，事务已回滚
            raise ValueError("配额余额不足，清缴失败，请重试")
        except Exception as exc:
            # 与并发首笔清缴撞幂等键：回滚并返回首笔记录
            if idempotency_key and is_duplicate_submit(exc):
                db.rollback()
                existing = _find_existing_clear(db, company_id, year, idempotency_key)
                if existing:
                    return existing
            raise
        return record


def settle_buyer_deficit_on_delivery(
    db: Session,
    order: "TradeOrder",
) -> ComplianceRecord | None:
    """订单交割联动清缴：用买方刚到账配额核销其同年度履约缺口（年度配额闭环）。

    必须在交割事务内、买卖双方账户入账完成之后调用：调用方已持有
    ``clear:<buyer>:<year>`` 与买方账户键（交割锁集合天然包含），
    因此这里不再重复加锁、也不自行开启事务，核销与交割同生共死。

    - 买方无活跃履约记录（尚无报告/清缴）：空操作，返回 None；
    - 优先核销已冻结配额，再用自由可用（含本次到账）补扣缺口；
    - 缺口大于到账量时只核销能覆盖的部分，履约记录保留 deficit；
    - 交割订单指定 ``auto_clear_deficit=0`` 时不联动，企业可另行手动清缴。
    """
    if not int(getattr(order, "auto_clear_deficit", 1) or 0):
        return None

    record = _get_active_record(db, order.buyer_id, order.year)
    if record is None:
        return None
    if record.status == "reversed":
        return None

    tx_date = order.tx_date or _today()
    record = _apply_clearance(
        db,
        order.buyer_id,
        order.year,
        tx_date,
        trade_order=order,
    )
    db.refresh(record)
    return record


def settle_buyer_deficit_on_auction(
    db: Session,
    trade: "AuctionTrade",
    tx_date: str,
    auto_clear: bool = True,
) -> ComplianceRecord | None:
    """集中竞价结算联动清缴：用买方到账配额核销其同年度履约缺口（年度配额闭环）。

    必须在结算事务内、买方全部成交单入账完成之后调用：调用方已持有
    ``clear:<buyer>:<year>`` 与买方账户键（结算锁集合天然包含），
    因此这里不再重复加锁、也不自行开启事务，核销与结算同生共死。

    - 买方无活跃履约记录（尚无报告/清缴）：空操作，返回 None；
    - 场次关闭 ``auto_clear_deficit`` 时不联动，企业可另行手动清缴。
    """
    if not auto_clear:
        return None

    record = _get_active_record(db, trade.buyer_id, trade.year)
    if record is None or record.status == "reversed":
        return None

    record = _apply_clearance(
        db,
        trade.buyer_id,
        trade.year,
        tx_date,
        auction_trade=trade,
    )
    db.refresh(record)
    return record


def settle_trade_deficit_on_auction(
    db: Session,
    trade: "AuctionTrade",
    tx_date: str,
    auto_clear: bool = True,
) -> ComplianceRecord | None:
    """单笔竞价成交结算后的逐笔联动清缴（同事务、无独立加锁）。

    与 :func:`settle_buyer_deficit_on_auction` 的区别：结算主流程对每一笔
    成交单划转后立即调用一次，使冻结核销/自由配额补缴流水都带上
    ``auction_trade_id``，监管冲正该成交单时可精确回滚它触发的清缴。
    同一买方多笔成交按 alloc_seq 依次核销，累计清缴仍不超过核查排放量。
    """
    if not auto_clear:
        return None
    record = _get_active_record(db, trade.buyer_id, trade.year)
    if record is None or record.status == "reversed":
        return None
    return _apply_clearance(
        db,
        trade.buyer_id,
        trade.year,
        tx_date,
        auction_trade=trade,
    )


def settle_borrower_deficit_on_loan(
    db: Session,
    loan: "QuotaLoan",
    tx_date: str,
    auto_clear: bool = True,
) -> ComplianceRecord | None:
    """借贷放款联动清缴：用借入方刚到账配额核销其同年度履约缺口（年度配额闭环）。

    必须在放款事务内、出借方出库/借入方到账完成之后调用：调用方已持有
    ``clear:<borrower>:<year>`` 与双方账户键（借贷锁集合天然包含），
    因此这里不再重复加锁、也不自行开启事务，核销与放款同生共死。

    - 借入方无活跃履约记录（尚无报告/清缴）：空操作，返回 None；
    - 优先核销已冻结配额（借入方自有冻结，流水不关联借贷单），再用自由可用
      （含本次借入到账）补扣缺口，补扣流水记为 ``loan_deficit_clear`` 并关联借贷单；
    - 借贷单关闭 ``auto_clear_deficit`` 时不联动，借入方自行决定清缴时机。
    """
    if not auto_clear:
        return None
    record = _get_active_record(db, loan.borrower_id, loan.year)
    if record is None or record.status == "reversed":
        return None
    return _apply_clearance(
        db,
        loan.borrower_id,
        loan.year,
        tx_date,
        quota_loan=loan,
    )
