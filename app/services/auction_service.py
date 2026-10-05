"""碳配额集中竞价市场：场次管理、密封报价、统一撮合、集中结算与权限审计。

业务状态机
==========
场次（AuctionSession）::

    draft ──open──▶ open ──match──▶ matched ──settle──▶ settled
      │                │                  │
      └──cancel────────┴────cancel────────┘
                        ▼
                     cancelled（撮合后撤场须逐笔释放卖方交易占用）

报价（AuctionBid）::

    active ──cancel──▶ cancelled（开放期，无账本副作用）
    active ──match───▶ matched / partial / unmatched（单次撮合定终态）

成交单（AuctionTrade）::

    reserved（撮合即占用卖方自由可用配额）──settle──▶ settled
                                          └─cancel──▶ cancelled（释放占用）
    settled ──reverse（足额收回）──▶ reversed
    settled ──reverse（买方持仓不足）──▶ defaulted ──补缴追偿结清──▶ reversed

已结算成交单的监管冲正与违约回退
================================
- reverse_settled_trades：整笔/批量/部分数量冲正，同一事务内回退双方配额划转、
  按成交单流水归属精确回滚联动清缴（先退自由补缴、后解除冻结），履约记录/配额
  状态同步回退，冲正单与审计同生共死；买方自由可用不足时只收回可得部分，不足
  登记违约欠额，卖方由解冻/退还的配额即时补位，不承担敞口；
- 违约追偿：recover_buyer_defaults / repay_trade_default 由监管手动触发，
  后续场次结算到账时在清缴后自动追偿（auto_recover_default）；买方自由可用
  划付受影响卖方，欠额结清后成交单 defaulted → reversed；
- 幂等：冲正批次与补缴记录均有幂等键唯一约束；场次键 + 账户/清缴键按锁序
  （account < auction < clear）串行化，并发重复冲正/追偿不会重复划转。


统一价格（uniform-price）双向竞价撮合
====================================
- 买入报价按价格降序、时间升序（id 升序）排队，卖出报价按价格升序、时间升序排队；
- 在每个候选价格 p 上：可行成交量 = min(买方中报价 ≥ p 的报量合计,
  卖方中报价 ≥ 保留价且报价 ≤ p 的报量合计)；
- 取可行成交量最大的价格为出清候选；多档等成交量时，按国内集合竞价惯例
  先选“未匹配量最小”档，仍并列则取候选档均价；无可行量则本场不成交；
- 成交分配遵循价格-时间优先：买方按队列逐单吃单，卖方按队列供货；
- 卖出报量先按其自由可用配额封顶（不得超卖），同一买方不与本企业自成交。

并发安全
========
- 场次键锁（auction:<id>）串行化该场次的一切写操作；所有竞价写流程统一按
  “先场次键（含历史违约场次，按键名排序）、后账户/清缴键（按名排序）”两层
  加锁，避免跨场次自动追偿时形成锁环；撮合/撤场额外取全部参与企业的账户键
  与清缴键；
- 撮合占用与结算划转复用账本原子条件 UPDATE：``current ≥ frozen + reserved``，
  履约冻结与交易占用（订单 + 竞价）互不可挤占；
- 结算/撤场用“状态必须为前置状态”的条件 UPDATE 抢占场次行，
  并发结算只有一个事务成功，其余幂等返回，绝不重复划转；
- 场次建场与报价均支持幂等键；状态抢占失败即整体回滚。

回写闭环
========
成交后写配额账户与交易流水（auction_bid_reserve/auction_bid_release/
auction_reserve_release/auction_deliver_out/auction_deliver_in/auction_deficit_clear），
并在同一事务内调用清缴内核核销买方同年度履约缺口（先冻结核销、再用到账配额补缴）；
全部敏感操作与越权拒绝写入权限审计日志。
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
    auction_session_key,
    company_clear_key,
    is_duplicate_submit,
    lock_row_for_write,
    locked_accounts,
    transactional,
)
from app.models.allowance import AllowanceAccount, AllowanceTransaction
from app.models.auction import (
    AuctionAuditLog,
    AuctionBid,
    AuctionDefaultRepayment,
    AuctionReversalBatch,
    AuctionSession,
    AuctionTrade,
    AuctionTradeReversal,
)
from app.models.company import Company
from app.services.loan_service import auto_recover_borrower_loans, overdue_loan_recovery_keys
from app.services.quota_service import settle_trade_deficit_on_auction

# 场次状态
DRAFT = "draft"
OPEN = "open"
MATCHED = "matched"
SETTLED = "settled"
CANCELLED = "cancelled"

# 报价状态
BID_ACTIVE = "active"
BID_MATCHED = "matched"
BID_PARTIAL = "partial"
BID_UNMATCHED = "unmatched"
BID_CANCELLED = "cancelled"

# 成交单状态
TRADE_RESERVED = "reserved"
TRADE_SETTLED = "settled"
TRADE_REVERSED = "reversed"
TRADE_DEFAULTED = "defaulted"
TRADE_CANCELLED = "cancelled"

_BUY = "buy"
_SELL = "sell"


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


class AuctionError(ValueError):
    """竞价业务规则不满足（场次/报价状态非法、越权、余额不足等）。"""


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
    session_id: int | None = None,
    detail: str = "",
    result: str = "success",
    commit: bool = False,
) -> AuctionAuditLog:
    """写入一条竞价权限审计日志。

    业务操作默认 ``commit=False``：审计日志与业务变更在同一事务提交（同生共死）；
    越权拒绝（access.denied）等没有业务事务的场景由调用方传 ``commit=True`` 立即落库。
    """
    log = AuctionAuditLog(
        operator_id=operator.id if operator else None,
        operator_name=operator.username if operator else "anonymous",
        operator_role=operator.role if operator else "",
        action=action,
        target_type=target_type,
        target_id=target_id,
        session_id=session_id,
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

def _get_session(db: Session, session_id: int) -> AuctionSession:
    session = db.get(AuctionSession, session_id)
    if session is None:
        raise AuctionError("竞价场次不存在")
    return session


def _get_bid(db: Session, bid_id: int) -> AuctionBid:
    bid = db.get(AuctionBid, bid_id)
    if bid is None:
        raise AuctionError("报价单不存在")
    return bid


def _get_account(db: Session, company_id: int, year: int) -> AllowanceAccount:
    account = (
        db.query(AllowanceAccount)
        .filter(AllowanceAccount.company_id == company_id, AllowanceAccount.year == year)
        .first()
    )
    if account is None:
        raise AuctionError(f"企业 {company_id} 的 {year} 年度配额账户不存在，请先完成配额分配")
    return account


def _company_name(db: Session, company_id: int) -> str:
    company = db.get(Company, company_id)
    return company.name if company else str(company_id)


def _gen_session_no(db: Session) -> str:
    count = db.query(AuctionSession).count()
    return f"AUC{datetime.utcnow().year}{count + 1:06d}"


def _gen_bid_no(db: Session) -> str:
    count = db.query(AuctionBid).count()
    return f"BID{count + 1:08d}"


def _gen_trade_no(db: Session) -> str:
    count = db.query(AuctionTrade).count()
    return f"AT{count + 1:08d}"


def _gen_batch_no(db: Session) -> str:
    count = db.query(AuctionReversalBatch).count()
    return f"ARV{datetime.utcnow().year}{count + 1:06d}{uuid.uuid4().hex[:6].upper()}"


def _gen_reversal_no(db: Session) -> str:
    count = db.query(AuctionTradeReversal).count()
    return f"ARD{count + 1:08d}{uuid.uuid4().hex[:4].upper()}"


def _gen_repay_no(db: Session) -> str:
    count = db.query(AuctionDefaultRepayment).count()
    return f"ARP{count + 1:08d}{uuid.uuid4().hex[:4].upper()}"


def _transit_session(
    db: Session,
    session_id: int,
    expected: tuple[str, ...],
    new_status: str,
) -> int:
    """场次状态条件 UPDATE：仅前置状态命中时流转，返回影响行数。

    多进程部署下进程锁无法互斥时，由数据库行更新做最后抢占，
    杜绝并发撮合/结算/撤场造成重复占用、重复划转。
    """
    result = db.execute(
        update(AuctionSession)
        .where(AuctionSession.id == session_id)
        .where(AuctionSession.status.in_(expected))
        .values(status=new_status)
        .execution_options(synchronize_session=False)
    )
    return result.rowcount


def _mark_trade_settled(db: Session, trade_id: int) -> None:
    """成交单结算落账：条件 UPDATE 抢占（reserved → settled）。

    循环内原子划转反复 expire_all，且流水对象反向引用成交单会触发 ORM
    懒刷新，ORM 属性赋值在 autoflush=False 下可能丢失；用数据库条件更新
    保证状态一定落库，影响行数为 0 即说明该单已被并发处理，整笔回滚。
    """
    result = db.execute(
        update(AuctionTrade)
        .where(AuctionTrade.id == trade_id)
        .where(AuctionTrade.status == TRADE_RESERVED)
        .values(status=TRADE_SETTLED, settled_at=datetime.utcnow())
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        raise AuctionError(f"成交单 {trade_id} 状态已变化，结算失败并整体回滚")


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
    trade: AuctionTrade,
    tx_date: str,
) -> AllowanceTransaction:
    tx = AllowanceTransaction(
        account_id=account.id,
        company_id=account.company_id,
        tx_type=tx_type,
        amount=round(amount, 4),
        counterparty=counterparty,
        price=round(float(trade.price), 2),
        tx_date=tx_date,
        balance_after=round(balance_after, 4),
        frozen_after=round(frozen_after, 4),
        reserved_after=round(reserved_after, 4),
        auction_trade_id=trade.id,
        remark=remark,
    )
    db.add(tx)
    return tx


def _add_ledger_no_trade(
    db: Session,
    account: AllowanceAccount,
    tx_type: str,
    amount: float,
    balance_after: float,
    frozen_after: float,
    reserved_after: float,
    counterparty: str,
    remark: str,
    tx_date: str,
) -> AllowanceTransaction:
    """报价占用/释放类流水（尚无成交单，不写 price/auction_trade_id）。"""
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
        remark=remark,
    )
    db.add(tx)
    return tx


# --------------------------------------------------------------------------- #
# 场次生命周期（监管）
# --------------------------------------------------------------------------- #

def create_session(
    db: Session,
    *,
    year: int,
    name: str,
    reserve_price: float = 0.0,
    estimated_volume: float | None = None,
    product: str = "allowance",
    auto_clear_deficit: bool = True,
    auto_recover_default: bool = True,
    remark: str = "",
    open_at: datetime | None = None,
    close_at: datetime | None = None,
    operator: Operator | None = None,
    idempotency_key: str | None = None,
) -> AuctionSession:
    """监管创建竞价场次（草稿）。携带相同幂等键的重复提交返回首场。

    传入 open_at 时创建即直接开放（监管可一步建场）。
    """
    if reserve_price < 0:
        raise AuctionError("保留价不能为负数")
    if estimated_volume is not None and float(estimated_volume) < 0:
        raise AuctionError("拟成交量不能为负数")
    if product not in ("allowance", "CCER"):
        raise AuctionError("品种标识非法")

    # 建场无既有行可锁：幂等由唯一约束（幂等键 / session_no）与数据库事务兜底
    if idempotency_key:
        existing = (
            db.query(AuctionSession)
            .filter(AuctionSession.idempotency_key == idempotency_key)
            .first()
        )
        if existing:
            return existing
    try:
        with transactional(db):
            direct_open = open_at is not None
            session = AuctionSession(
                session_no=_gen_session_no(db),
                name=(name or "").strip()[:128],
                year=year,
                product=product,
                reserve_price=round(reserve_price, 2),
                estimated_volume=round(float(estimated_volume), 4) if estimated_volume is not None else None,
                status=OPEN if direct_open else DRAFT,
                auto_clear_deficit=1 if auto_clear_deficit else 0,
                auto_recover_default=1 if auto_recover_default else 0,
                open_at=open_at if direct_open else None,
                close_at=close_at,
                created_by=operator.id if operator else None,
                remark=(remark or "").strip()[:256],
                idempotency_key=idempotency_key,
            )
            db.add(session)
            db.flush()
            write_audit(
                db, operator,
                "session.open" if direct_open else "session.create",
                target_type="session", target_id=session.id, session_id=session.id,
                detail=f"创建竞价场次 {session.session_no}（{year}年度，保留价 {reserve_price}）"
                + ("，直接开放报价" if direct_open else ""),
            )
            db.refresh(session)
    except IntegrityError as exc:
        if idempotency_key and is_duplicate_submit(exc):
            db.rollback()
            existing = (
                db.query(AuctionSession)
                .filter(AuctionSession.idempotency_key == idempotency_key)
                .first()
            )
            if existing:
                return existing
        raise
    return session


def open_session(db: Session, session_id: int, operator: Operator | None) -> AuctionSession:
    """监管开放场次报价。仅草稿可开放；重复开放幂等。"""
    with locked_accounts([auction_session_key(session_id)]):
        session = _get_session(db, session_id)
        if session.status == OPEN:
            db.refresh(session)
            return session
        if session.status != DRAFT:
            raise AuctionError(f"场次当前为 {session.status}，不能开放报价")
        with transactional(db):
            if _transit_session(db, session_id, (DRAFT,), OPEN) != 1:
                raise AuctionError("场次状态已变化，开放失败，请刷新后重试")
            session = _get_session(db, session_id)
            session.open_at = datetime.utcnow()
            write_audit(
                db, operator, "session.open",
                target_type="session", target_id=session_id, session_id=session_id,
                detail=f"开放场次 {session.session_no} 报价",
            )
            db.flush()
            db.refresh(session)
        return session


def cancel_session(
    db: Session,
    session_id: int,
    operator: Operator | None,
    reason: str = "",
) -> AuctionSession:
    """监管撤场。

    - draft 撤场：无报价/无账本副作用；
    - open 撤场：active 卖出报价逐笔释放报价占用（auction_bid_release），
      全部报价置 cancelled；
    - matched 撤场：逐笔释放成交单占用（auction_reserve_release），
      成交单置 cancelled，已撮合报价回到 cancelled；
    - settled 场次终态不可撤。
    """
    reason = (reason or "").strip()[:256]
    with locked_accounts([auction_session_key(session_id)]):
        session = _get_session(db, session_id)
        if session.status == CANCELLED:
            db.refresh(session)
            return session
        if session.status == SETTLED:
            raise AuctionError("场次已结算，不能撤销")
        if session.status not in (DRAFT, OPEN, MATCHED):
            raise AuctionError(f"场次状态 {session.status} 不可撤销")

        # 需要释放占用的卖方账户：
        # - MATCHED：尚有 reserved 成交单的卖方；
        # - OPEN：仍有 active 卖出报价（报价时已占用）的卖方。
        trades: list[AuctionTrade] = []
        active_sell_bids: list[AuctionBid] = []
        if session.status == MATCHED:
            trades = (
                db.query(AuctionTrade)
                .filter(AuctionTrade.session_id == session_id, AuctionTrade.status == TRADE_RESERVED)
                .all()
            )
        elif session.status == OPEN:
            active_sell_bids = (
                db.query(AuctionBid)
                .filter(
                    AuctionBid.session_id == session_id,
                    AuctionBid.status == BID_ACTIVE,
                    AuctionBid.side == _SELL,
                )
                .all()
            )

        release_company_ids = {t.seller_id for t in trades} | {b.company_id for b in active_sell_bids}
        keys: list[str] = []
        accounts: dict[int, AllowanceAccount] = {}
        for cid in release_company_ids:
            acc = _get_account(db, cid, session.year)
            accounts[cid] = acc
            keys.append(account_lock_key(acc.id))
            keys.append(company_clear_key(cid, session.year))
        # 场次键已持有（外层），账户/清缴键按序加锁防死锁
        with locked_accounts(sorted(set(keys))):
            try:
                with transactional(db):
                    if _transit_session(db, session_id, (DRAFT, OPEN, MATCHED), CANCELLED) != 1:
                        raise AuctionError("场次状态已变化，撤场失败，请刷新后重试")
                    tx_date = _today()

                    if session.status == MATCHED and trades:
                        for trade_ref in trades:
                            trade = db.get(AuctionTrade, trade_ref.id)
                            seller_acc = lock_row_for_write(db, accounts[trade.seller_id].id)
                            accounts[trade.seller_id] = seller_acc
                            qty = _round4(trade.quantity)
                            bal, frz, rsv = apply_ledger_delta(db, seller_acc.id, 0, 0, -qty)
                            buyer_name = _company_name(db, trade.buyer_id)
                            _add_ledger(
                                db, seller_acc, "auction_reserve_release", qty,
                                bal, frz, rsv, buyer_name,
                                f"场次 {session.session_no} 撤场，释放成交单 {trade.trade_no} 占用 {qty} 吨",
                                trade, tx_date,
                            )
                            # apply_ledger_delta 内部 expire_all，状态赋值前重取
                            trade = db.get(AuctionTrade, trade.id)
                            trade.status = TRADE_CANCELLED
                            trade.cancelled_at = datetime.utcnow()

                        # 已撮合报价随撤场回到取消
                        db.execute(
                            update(AuctionBid)
                            .where(
                                AuctionBid.session_id == session_id,
                                AuctionBid.status.in_([BID_MATCHED, BID_PARTIAL]),
                            )
                            .values(
                                status=BID_CANCELLED,
                                cancel_reason="场次撤场，占用配额已释放",
                                cancelled_at=datetime.utcnow(),
                            )
                            .execution_options(synchronize_session=False)
                        )
                        # 无成交（unmatched）报价无需释放占用（撮合时已释放），仅标记取消
                        db.execute(
                            update(AuctionBid)
                            .where(
                                AuctionBid.session_id == session_id,
                                AuctionBid.status == BID_UNMATCHED,
                            )
                            .values(
                                status=BID_CANCELLED,
                                cancel_reason="场次撤场",
                                cancelled_at=datetime.utcnow(),
                            )
                            .execution_options(synchronize_session=False)
                        )

                    if session.status == OPEN:
                        # 释放每张有效卖出报价在报价时占用的配额
                        for bid in active_sell_bids:
                            acc = lock_row_for_write(db, accounts[bid.company_id].id)
                            accounts[bid.company_id] = acc
                            qty = _num(bid.quantity)
                            bal, frz, rsv = apply_ledger_delta(db, acc.id, 0, 0, -qty)
                            _add_ledger_no_trade(
                                db, acc, "auction_bid_release", qty,
                                bal, frz, rsv, "竞价撤场",
                                f"场次 {session.session_no} 撤场，释放卖出报价 {bid.bid_no} 占用 {qty} 吨",
                                tx_date,
                            )

                    # 开放期全部 active 报价（买/卖）直接置撤单
                    db.execute(
                        update(AuctionBid)
                        .where(
                            AuctionBid.session_id == session_id,
                            AuctionBid.status == BID_ACTIVE,
                        )
                        .values(
                            status=BID_CANCELLED,
                            cancel_reason=reason or "场次取消",
                            cancelled_at=datetime.utcnow(),
                        )
                        .execution_options(synchronize_session=False)
                    )

                    session = _get_session(db, session_id)
                    session.cancel_reason = reason
                    session.cancelled_by = operator.id if operator else None
                    session.cancelled_at = datetime.utcnow()
                    write_audit(
                        db, operator, "session.cancel",
                        target_type="session", target_id=session_id, session_id=session_id,
                        detail=f"撤销场次 {session.session_no}：{reason or '（无原因）'}"
                        + (f"，释放成交单 {len(trades)} 笔" if trades else "")
                        + (f"，释放有效卖出报价 {len(active_sell_bids)} 张" if active_sell_bids else ""),
                    )
                    db.flush()
                    db.refresh(session)
            except InsufficientBalanceError:
                raise AuctionError("释放竞价占用失败，账本状态异常，撤场已回滚")
            return session


# --------------------------------------------------------------------------- #
# 报价与撤单（买/卖方企业，或监管代操作）
# --------------------------------------------------------------------------- #

def place_bid(
    db: Session,
    session_id: int,
    company_id: int,
    side: str,
    quantity: float,
    price: float,
    *,
    tx_date: str = "",
    remark: str = "",
    operator: Operator | None = None,
    idempotency_key: str | None = None,
) -> AuctionBid:
    """在开放场次内提交密封报价。

    - side=sell 卖出：报量立即从自由可用配额转为交易占用（reserved），
      与企业间订单占用同一套账本不变量，杜绝“报价后配额被他用”的超卖竞态；
      撤单/未成交/部分成交余量按对应流程释放，成交部分结算时出库；
    - side=buy 买入：无配额校验（资金侧不在本台账范围）；
    - 同一企业同一场次同一方向只允许一张有效报价（部分唯一索引 + 键锁兜底）；
    - 报价价格不得低于场次保留价（买卖双方均以保留价为有效报价下限）。
    """
    if side not in (_BUY, _SELL):
        raise AuctionError("报价方向非法（buy/sell）")
    if quantity <= 0:
        raise AuctionError("报价数量必须为正数")
    if price < 0:
        raise AuctionError("报价单价不能为负数")
    if not db.get(Company, company_id):
        raise AuctionError("报价企业不存在")

    session = _get_session(db, session_id)
    if session.status != OPEN:
        raise AuctionError(f"场次当前为 {session.status}，仅开放场次可报价")
    if price < float(session.reserve_price or 0) - 1e-9:
        raise AuctionError(f"报价不得低于场次保留价 {float(session.reserve_price):.2f} 元/吨")

    account = _get_account(db, company_id, session.year)

    with locked_accounts([auction_session_key(session_id), account_lock_key(account.id)]):
        session = _get_session(db, session_id)
        if session.status != OPEN:
            raise AuctionError("场次已结束报价，提交被拒绝")

        # 幂等命中优先返回（双击/重试），避免把合法重试误判为重复报价
        if idempotency_key:
            same = (
                db.query(AuctionBid)
                .filter(AuctionBid.idempotency_key == idempotency_key)
                .first()
            )
            if same:
                return same

        existing = (
            db.query(AuctionBid)
            .filter(
                AuctionBid.session_id == session_id,
                AuctionBid.company_id == company_id,
                AuctionBid.side == side,
                AuctionBid.status == BID_ACTIVE,
            )
            .first()
        )
        if existing:
            raise AuctionError("本企业在该场次同方向已有有效报价，请先撤单后重新报价")

        qty = round(float(quantity), 4)
        try:
            with transactional(db):
                # 卖出报价原子占用：原子条件 UPDATE 保证
                # current >= frozen + reserved，跨场次/订单并发也不会超卖
                if side == _SELL:
                    account = lock_row_for_write(db, account.id)
                    available = round(
                        float(account.current_balance)
                        - float(account.frozen_balance)
                        - float(account.reserved_balance),
                        4,
                    )
                    if available + 1e-9 < qty:
                        raise InsufficientBalanceError(
                            f"自由可用配额不足：最多可报 {available:g} 吨"
                        )
                    bal, frz, rsv = apply_ledger_delta(db, account.id, 0, 0, qty)

                bid = AuctionBid(
                    bid_no=_gen_bid_no(db),
                    session_id=session_id,
                    company_id=company_id,
                    side=side,
                    year=session.year,
                    quantity=qty,
                    price=round(float(price), 2),
                    tx_date=tx_date or _today(),
                    remark=(remark or "").strip()[:256],
                    created_by=operator.id if operator else None,
                    idempotency_key=idempotency_key,
                )
                db.add(bid)
                db.flush()

                if side == _SELL:
                    _add_ledger_no_trade(
                        db, account, "auction_bid_reserve", qty,
                        bal, frz, rsv, "竞价报价",
                        f"场次 {session.session_no} 卖出报价 {bid.bid_no} 冻结/占用 {qty} 吨 @ {price:.2f}",
                        bid.tx_date,
                    )

                write_audit(
                    db, operator, "bid.place",
                    target_type="bid", target_id=bid.id, session_id=session_id,
                    detail=f"企业 {_company_name(db, company_id)} "
                    f"{'买入' if side == _BUY else '卖出'}报价 {qty:g} 吨 @ {price:.2f}，报价单 {bid.bid_no}",
                )
                db.refresh(bid)
        except InsufficientBalanceError:
            raise AuctionError(
                f"自由可用配额不足（已扣除履约冻结与交易占用），无法报卖 {qty:g} 吨"
            )
        except IntegrityError as exc:
            if idempotency_key and is_duplicate_submit(exc):
                db.rollback()
                same = (
                    db.query(AuctionBid)
                    .filter(AuctionBid.idempotency_key == idempotency_key)
                    .first()
                )
                if same:
                    return same
            # 并发报价撞“同企业同方向有效报价唯一”索引
            if "uq_auction_active_bid" in str(getattr(exc, "orig", exc)) or (
                "UNIQUE constraint failed" in str(getattr(exc, "orig", exc))
                and "auction_bids" in str(getattr(exc, "orig", exc))
            ):
                raise AuctionError("本企业在该场次同方向已有有效报价，请勿重复提交")
            raise
        return bid


def cancel_bid(
    db: Session,
    bid_id: int,
    company_id: int | None,
    operator: Operator | None,
    reason: str = "",
    *,
    as_regulator: bool = False,
) -> AuctionBid:
    """撤销报价。

    - 企业只能撤销本企业报价（company_id 即登录企业）；
    - 监管（as_regulator=True）可撤销任意企业报价；
    - 仅 active 报价可撤；撮合后报价为终态不可撤（须监管撤场）。
    """
    bid = _get_bid(db, bid_id)
    if not as_regulator and company_id != bid.company_id:
        raise AuctionError("无权撤销其他企业的报价")
    if bid.status != BID_ACTIVE:
        raise AuctionError(f"报价当前为 {bid.status}，不可撤销")

    account = _get_account(db, bid.company_id, bid.year)
    with locked_accounts([auction_session_key(bid.session_id), account_lock_key(account.id)]):
        bid = _get_bid(db, bid_id)
        if bid.status == BID_CANCELLED:
            db.refresh(bid)
            return bid
        if bid.status != BID_ACTIVE:
            raise AuctionError("报价已撮合，不可单独撤销；如需终止请由监管撤场")

        try:
            with transactional(db):
                result = db.execute(
                    update(AuctionBid)
                    .where(AuctionBid.id == bid_id, AuctionBid.status == BID_ACTIVE)
                    .values(
                        status=BID_CANCELLED,
                        cancel_reason=(reason or "").strip()[:256],
                        cancelled_by=operator.id if operator else None,
                        cancelled_at=datetime.utcnow(),
                    )
                    .execution_options(synchronize_session=False)
                )
                if result.rowcount != 1:
                    raise AuctionError("报价状态已变化，撤单失败，请刷新后重试")

                # 卖出报价占用的配额当场释放回自由可用（买入报价无占用）
                if bid.side == _SELL:
                    account = lock_row_for_write(db, account.id)
                    qty = _num(bid.quantity)
                    bal, frz, rsv = apply_ledger_delta(db, account.id, 0, 0, -qty)
                    _add_ledger_no_trade(
                        db, account, "auction_bid_release", qty,
                        bal, frz, rsv, "竞价撤单",
                        f"撤销卖出报价 {bid.bid_no}，释放占用 {qty} 吨",
                        bid.tx_date or _today(),
                    )

                write_audit(
                    db, operator, "bid.cancel",
                    target_type="bid", target_id=bid_id, session_id=bid.session_id,
                    detail=("监管" if as_regulator else "企业")
                    + f"撤销报价 {bid.bid_no}（{_company_name(db, bid.company_id)}，"
                    + f"{'买入' if bid.side == _BUY else '卖出'} {_num(bid.quantity):g} 吨）：{reason or '（无原因）'}",
                )
                db.flush()
                db.refresh(bid)
        except InsufficientBalanceError:
            raise AuctionError("释放报价占用失败，账本状态异常，撤单已回滚")
        return bid


# --------------------------------------------------------------------------- #
# 撮合（统一价格双向竞价）
# --------------------------------------------------------------------------- #

def _candidate_prices(buys: list[AuctionBid], sells: list[AuctionBid], reserve_price: float) -> list[float]:
    """候选出清价：所有不低于保留价的买卖报价档位（集合竞价只在报价点上出清）。"""
    prices = {
        round(float(b.price), 2)
        for b in (*buys, *sells)
        if float(b.price) + 1e-9 >= reserve_price
    }
    return sorted(prices)


def _allocate_at_price(
    buy_queue: list[dict],
    sell_queue: list[dict],
) -> list[dict]:
    """在给定价格已筛好的买卖队列上做价格-时间优先配对（不与本企业自成交）。

    买方队列按价格降序、时间升序；卖方队列按价格升序、时间升序。
    每个买方依次在卖方队列中寻找第一家“非本企业且有余额”的卖方成交；
    找不到则把该买方需求量留给后续卖方（继续尝试下一个买方，保证不遗漏
    “当前买方自相关、但后续买方可成交”的供货）。

    返回 ``[{buyer, seller, quantity}, ...]``；同时原地扣减各队列 left。
    """
    pairs: list[dict] = []
    for bq in buy_queue:
        while bq["left"] > 1e-9:
            sq = next(
                (
                    x
                    for x in sell_queue
                    if x["left"] > 1e-9 and x["bid"].company_id != bq["bid"].company_id
                ),
                None,
            )
            if sq is None:
                break
            qty = round(min(bq["left"], sq["left"]), 4)
            pairs.append({"buyer": bq, "seller": sq, "quantity": qty})
            bq["left"] = round(bq["left"] - qty, 4)
            sq["left"] = round(sq["left"] - qty, 4)
    return pairs


def _simulate_volume(
    buys: list[AuctionBid],
    sells: list[AuctionBid],
    price: float,
) -> tuple[float, float, float]:
    """在候选价上模拟配对，返回 (实际可成交量, 买方申报需求, 卖方申报供给)。

    与 :func:`_allocate_at_price` 使用完全相同的配对规则与自成交规避，
    保证选出的出清价一定可以实际执行（不会“有价无量”）。
    """
    bq = [
        {"bid": b, "left": _num(b.quantity)}
        for b in buys
        if float(b.price) + 1e-9 >= price
    ]
    sq = [
        {"bid": s, "left": _num(s.quantity)}
        for s in sells
        if float(s.price) <= price + 1e-9
    ]
    pairs = _allocate_at_price(bq, sq)
    volume = round(sum(p["quantity"] for p in pairs), 4)
    demand = round(sum(_num(b.quantity) for b in buys if float(b.price) + 1e-9 >= price), 4)
    supply = round(sum(_num(s.quantity) for s in sells if float(s.price) <= price + 1e-9), 4)
    return volume, demand, supply


def _determine_clear_price(
    buys: list[AuctionBid],
    sells: list[AuctionBid],
    reserve_price: float,
) -> tuple[float, float] | None:
    """计算统一出清价，返回 ``(出清价, 实际可成交量)``；无可行量返回 None。

    规则：
    1. 最大化实际可成交量（价格-时间优先且规避自成交后的配对量）；
    2. 并列时选未匹配量（|需求-供给|）最小档；
    3. 仍并列取候选档均价（保留两位小数，国内集合竞价惯例）。
    """
    candidates = _candidate_prices(buys, sells, reserve_price)
    best: list[tuple[float, float, float]] = []  # (可成交量, 未匹配量, 价格)
    max_volume = 0.0
    for p in candidates:
        volume, demand, supply = _simulate_volume(buys, sells, p)
        if volume > max_volume + 1e-9:
            max_volume = volume
            best = [(volume, abs(demand - supply), p)]
        elif abs(volume - max_volume) <= 1e-9 and volume > 0:
            best.append((volume, abs(demand - supply), p))
    if not best or max_volume <= 0:
        return None
    min_imbalance = min(x[1] for x in best)
    winners = [x[2] for x in best if abs(x[1] - min_imbalance) <= 1e-9]
    clear_price = round(sum(winners) / len(winners), 2)
    return clear_price, max_volume


def run_matching(db: Session, session_id: int, operator: Operator | None) -> AuctionSession:
    """监管触发统一撮合：定价、价格-时间优先配对、调整卖方占用、生成成交单。

    撮合是单次密封集合竞价：一次撮合后全部报价进入终态（matched/partial/unmatched）。
    卖出报价在提交时已把报量转为交易占用（reserved），撮合阶段只做占用归属调整：
    成交部分保留占用等待结算出库，未成交/未成交余量当场释放回自由可用。
    重复撮合幂等返回。
    """
    with locked_accounts([auction_session_key(session_id)]):
        session = _get_session(db, session_id)
        if session.status == MATCHED:
            db.refresh(session)
            return session
        if session.status == SETTLED:
            raise AuctionError("场次已结算，不能重复撮合")
        if session.status != OPEN:
            raise AuctionError(f"场次当前为 {session.status}，不能撮合")

        bids = (
            db.query(AuctionBid)
            .filter(AuctionBid.session_id == session_id, AuctionBid.status == BID_ACTIVE)
            .all()
        )
        buys = sorted(
            [b for b in bids if b.side == _BUY],
            key=lambda b: (-float(b.price), b.id),
        )
        sells = sorted(
            [b for b in bids if b.side == _SELL],
            key=lambda b: (float(b.price), b.id),
        )

        # 撮合只触碰卖出方账户（释放未成交占用）；账户与场次同事务锁定
        company_ids = {b.company_id for b in sells}
        accounts: dict[int, AllowanceAccount] = {}
        keys: list[str] = []
        for cid in company_ids:
            acc = _get_account(db, cid, session.year)
            accounts[cid] = acc
            keys.append(account_lock_key(acc.id))
            keys.append(company_clear_key(cid, session.year))

        with locked_accounts(sorted(set(keys))):
            try:
                with transactional(db):
                    if _transit_session(db, session_id, (OPEN,), MATCHED) != 1:
                        raise AuctionError("场次状态已变化，撮合失败，请刷新后重试")
                    session = _get_session(db, session_id)
                    reserve_price = float(session.reserve_price or 0)
                    now = datetime.utcnow()
                    tx_date = _today()

                    pricing = _determine_clear_price(buys, sells, reserve_price)

                    if pricing is None:
                        # 无可行成交：释放全部卖出报价占用，全部报价置未成交
                        for sbid in sells:
                            acc = lock_row_for_write(db, accounts[sbid.company_id].id)
                            accounts[sbid.company_id] = acc
                            qty = _num(sbid.quantity)
                            bal, frz, rsv = apply_ledger_delta(db, acc.id, 0, 0, -qty)
                            _add_ledger_no_trade(
                                db, acc, "auction_bid_release", qty, bal, frz, rsv,
                                "竞价撤单", f"场次 {session.session_no} 无成交，释放卖出报价 {sbid.bid_no} 占用 {qty} 吨",
                                tx_date,
                            )
                        db.execute(
                            update(AuctionBid)
                            .where(
                                AuctionBid.session_id == session_id,
                                AuctionBid.status == BID_ACTIVE,
                            )
                            .values(status=BID_UNMATCHED, matched_at=now)
                            .execution_options(synchronize_session=False)
                        )
                        session = _get_session(db, session_id)
                        session.matched_at = now
                        session.clear_price = None
                        session.matched_volume = 0
                        write_audit(
                            db, operator, "session.match",
                            target_type="session", target_id=session_id, session_id=session_id,
                            detail=f"场次 {session.session_no} 撮合完成：无可成交报价（{len(buys)} 买 / {len(sells)} 卖）",
                        )
                        db.flush()
                        db.refresh(session)
                        return session

                    clear_price, _ = pricing

                    # 出清价下的有效队列（实际配对器，与定价模拟完全一致）
                    buy_queue = [
                        {"bid": b, "left": _num(b.quantity)}
                        for b in buys
                        if float(b.price) + 1e-9 >= clear_price
                    ]
                    sell_queue = [
                        {"bid": s, "left": _num(s.quantity)}
                        for s in sells
                        if float(s.price) <= clear_price + 1e-9
                    ]
                    pairs = _allocate_at_price(buy_queue, sell_queue)

                    trades: list[AuctionTrade] = []
                    filled_by_bid: dict[int, float] = {}
                    sell_unreleased: dict[int, float] = {}  # 卖方已成交、保留占用的数量
                    for seq, pair in enumerate(pairs, start=1):
                        bq, sq, qty = pair["buyer"], pair["seller"], pair["quantity"]
                        trade = AuctionTrade(
                            trade_no=_gen_trade_no(db),
                            session_id=session_id,
                            buyer_bid_id=bq["bid"].id,
                            seller_bid_id=sq["bid"].id,
                            buyer_id=bq["bid"].company_id,
                            seller_id=sq["bid"].company_id,
                            year=session.year,
                            quantity=qty,
                            price=clear_price,
                            alloc_seq=seq,
                            status=TRADE_RESERVED,
                        )
                        db.add(trade)
                        db.flush()
                        filled_by_bid[bq["bid"].id] = round(filled_by_bid.get(bq["bid"].id, 0.0) + qty, 4)
                        filled_by_bid[sq["bid"].id] = round(filled_by_bid.get(sq["bid"].id, 0.0) + qty, 4)
                        sell_unreleased[sq["bid"].id] = round(
                            sell_unreleased.get(sq["bid"].id, 0.0) + qty, 4
                        )
                        trades.append(trade)

                    # 卖方占用归属调整：未成交/部分成交的报价余量当场释放回自由可用
                    for sbid in sells:
                        filled = round(filled_by_bid.get(sbid.id, 0.0), 4)
                        leftover = round(_num(sbid.quantity) - filled, 4)
                        if leftover > 1e-9:
                            acc = lock_row_for_write(db, accounts[sbid.company_id].id)
                            accounts[sbid.company_id] = acc
                            bal, frz, rsv = apply_ledger_delta(db, acc.id, 0, 0, -leftover)
                            _add_ledger_no_trade(
                                db, acc, "auction_bid_release", leftover, bal, frz, rsv,
                                "竞价撮合",
                                f"场次 {session.session_no} 撮合，卖出报价 {sbid.bid_no} 未成交余量 "
                                f"{leftover} 吨释放",
                                tx_date,
                            )

                    # 回写报价成交状态（循环内 apply_ledger_delta 会 expire_all，
                    # 重新从身份映射取报价对象，避免状态赋值丢失）
                    for b_ref in (*buys, *sells):
                        b = db.get(AuctionBid, b_ref.id)
                        filled = round(filled_by_bid.get(b.id, 0.0), 4)
                        if filled <= 0:
                            b.status = BID_UNMATCHED
                        elif filled + 1e-9 >= _num(b.quantity):
                            b.status = BID_MATCHED
                            b.filled_quantity = _num(b.quantity)
                        else:
                            b.status = BID_PARTIAL
                            b.filled_quantity = filled
                        b.matched_at = now

                    total_volume = round(sum(_num(t.quantity) for t in trades), 4)
                    session = _get_session(db, session_id)
                    session.clear_price = clear_price
                    session.matched_volume = total_volume
                    session.trade_count = len(trades)
                    session.matched_at = now
                    write_audit(
                        db, operator, "session.match",
                        target_type="session", target_id=session_id, session_id=session_id,
                        detail=f"场次 {session.session_no} 撮合完成：出清价 {clear_price:.2f} 元/吨，"
                        f"成交 {len(trades)} 笔 / {total_volume:g} 吨",
                    )
                    db.flush()
                    db.refresh(session)
            except InsufficientBalanceError:
                raise AuctionError("撮合调整卖方占用失败，撮合已整体回滚")
            return session


# --------------------------------------------------------------------------- #
# 结算（并发安全：状态抢占，只有一个事务成功）
# --------------------------------------------------------------------------- #

def settle_session(db: Session, session_id: int, operator: Operator | None) -> AuctionSession:
    """监管触发场次统一结算：卖方占用出库、买方到账、买方缺口核销同一事务完成。

    已结算场次重复调用幂等返回；撮合/撤场与结算并发时由场次状态条件 UPDATE 抢占。
    """
    with locked_accounts([auction_session_key(session_id)]):
        session = _get_session(db, session_id)
        if session.status == SETTLED:
            db.refresh(session)
            return session
        if session.status == CANCELLED:
            raise AuctionError("场次已撤销，不能结算")
        if session.status != MATCHED:
            raise AuctionError(f"场次当前为 {session.status}，尚未撮合，不能结算")

        trades = (
            db.query(AuctionTrade)
            .filter(AuctionTrade.session_id == session_id)
            .order_by(AuctionTrade.alloc_seq.asc(), AuctionTrade.id.asc())
            .all()
        )
        reserved_trades = [t for t in trades if t.status == TRADE_RESERVED]

        # 自动追偿可能触碰买方历史违约对应的历史场次键：先收集全部场次键，
        # 按竞价统一锁序（先场次、后账户/清缴）两层加锁，杜绝跨场次锁环。
        company_ids = {c for t in reserved_trades for c in (t.seller_id, t.buyer_id)}
        prior_session_ids: set[int] = set()
        if bool(int(session.auto_recover_default or 0)):
            prior = (
                db.query(AuctionTrade)
                .filter(
                    AuctionTrade.buyer_id.in_(company_ids),
                    AuctionTrade.year == session.year,
                    AuctionTrade.defaulted_amount > AuctionTrade.repaid_amount,
                )
                .all()
            )
            prior_session_ids = {t.session_id for t in prior}
            company_ids |= {t.seller_id for t in prior}

        session_keys = sorted({auction_session_key(sid)
                               for sid in prior_session_ids | {session_id}})

        # 收集全部买卖双方（含历史违约卖方）账户键与清缴键
        accounts: dict[int, AllowanceAccount] = {}
        account_keys: list[str] = []
        for cid in company_ids:
            acc = _get_account(db, cid, session.year)
            accounts[cid] = acc
            account_keys.append(account_lock_key(acc.id))
            account_keys.append(company_clear_key(cid, session.year))

        # 结算到账后可能自动追偿买方历史逾期/违约借贷欠额：把相关借贷键与
        # 受影响出借方账户/清缴键并入统一锁集合（键名字典序满足
        # account < auction < clear < loan < order，不会形成锁环）。
        loan_keys: set[str] = set()
        for buyer_id in company_ids:
            for key in overdue_loan_recovery_keys(db, buyer_id, session.year):
                loan_keys.add(key)

        with locked_accounts(session_keys):
            with locked_accounts(sorted(set(account_keys) | loan_keys)):
                try:
                    with transactional(db):
                        if _transit_session(db, session_id, (MATCHED,), SETTLED) != 1:
                            raise AuctionError("场次状态已变化，结算失败，请刷新后重试")
                        session = _get_session(db, session_id)
                        tx_date = _today()
                        auto_clear = bool(int(session.auto_clear_deficit or 0))

                        buyer_ids: set[int] = set()
                        for trade in reserved_trades:
                            seller_acc = lock_row_for_write(db, accounts[trade.seller_id].id)
                            accounts[trade.seller_id] = seller_acc
                            buyer_acc = lock_row_for_write(db, accounts[trade.buyer_id].id)
                            accounts[trade.buyer_id] = buyer_acc
                            # apply_ledger_delta 内部 expire_all 会使本循环先前取到的
                            # trade 对象过期，状态赋值将丢失；每笔重新取身份映射对象。
                            trade = db.get(AuctionTrade, trade.id)
                            qty = _round4(trade.quantity)

                            buyer_name = _company_name(db, trade.buyer_id)
                            seller_name = _company_name(db, trade.seller_id)

                            # 卖方：撮合占用配额正式出库（current/reserved 同减，frozen 不变）
                            s_bal, s_frz, s_rsv = apply_ledger_delta(
                                db, seller_acc.id, -qty, 0, -qty
                            )
                            _add_ledger(
                                db, seller_acc, "auction_deliver_out", qty,
                                s_bal, s_frz, s_rsv, buyer_name,
                                f"场次 {session.session_no} 结算划出 {qty} 吨 @ {float(trade.price):.2f}，"
                                f"成交单 {trade.trade_no}",
                                trade, tx_date,
                            )

                            # 买方：配额到账（不动既有冻结/占用）
                            b_bal, b_frz, b_rsv = apply_ledger_delta(db, buyer_acc.id, qty, 0, 0)
                            _add_ledger(
                                db, buyer_acc, "auction_deliver_in", qty,
                                b_bal, b_frz, b_rsv, seller_name,
                                f"场次 {session.session_no} 结算受让 {qty} 吨 @ {float(trade.price):.2f}，"
                                f"成交单 {trade.trade_no}",
                                trade, tx_date,
                            )

                            # 逐笔联动核销买方缺口：冻结/补缴流水均归属本成交单，
                            # 监管冲正成交单时才能精确回滚该笔成交触发的清缴。
                            settle_trade_deficit_on_auction(
                                db, trade, tx_date, auto_clear=auto_clear
                            )

                            # 条件 UPDATE 抢占落 settled（循环内 expire_all + 流水
                            # 反向引用会使 ORM 赋值丢失，数据库更新是唯一可靠路径）。
                            _mark_trade_settled(db, trade.id)
                            buyer_ids.add(trade.buyer_id)

                        # 成交单状态为 ORM 赋值，autoflush=False 的调用方下后续查询
                        # （如自动追偿过滤 defaulted 成交单）需要看到终态，显式 flush。
                        db.flush()

                        # 违约回退链路：到账且缺口核销完成后，自动用买方剩余自由可用
                        # 配额追偿其历史违约欠额（先清缴后追偿，绝不挪用履约配额）。
                        recovered_count = 0
                        if bool(int(session.auto_recover_default or 0)):
                            for buyer_id in buyer_ids:
                                summary = _recover_buyer_arrears(
                                    db, buyer_id, session.year, tx_date,
                                    source="auto", session=session,
                                )
                                recovered_count += len(summary["repayments"])

                        # 借贷到期清偿联动：同样在清缴之后，用买方剩余自由可用自动
                        # 追偿其逾期/违约借贷欠额（先清缴后追偿，不挪用履约配额）。
                        loan_recovered_count = 0
                        for buyer_id in buyer_ids:
                            summary = auto_recover_borrower_loans(
                                db, buyer_id, session.year, tx_date
                            )
                            loan_recovered_count += len(summary["repayments"])

                        # 买方年度配额闭环：逐笔成交逐笔核销（先冻结后补缴，绝不触碰占用）
                        clearance_count = sum(
                            1 for t in reserved_trades
                            if db.query(AllowanceTransaction).filter_by(
                                auction_trade_id=t.id,
                                tx_type="auction_deficit_clear").count() > 0
                            or db.query(AllowanceTransaction).filter(
                                AllowanceTransaction.auction_trade_id == t.id,
                                AllowanceTransaction.tx_type == "frozen_clear").count() > 0
                        )

                        session = _get_session(db, session_id)
                        session.settled_at = datetime.utcnow()
                        write_audit(
                            db, operator, "session.settle",
                            target_type="session", target_id=session_id, session_id=session_id,
                            detail=f"场次 {session.session_no} 统一结算：成交 {len(reserved_trades)} 笔 / "
                            f"{_num(session.matched_volume):g} 吨全部划转，联动核销买方缺口 {clearance_count} 家"
                            + (f"，自动追偿违约欠额 {recovered_count} 笔" if recovered_count else "")
                            + (f"，自动追偿借贷欠额 {loan_recovered_count} 笔" if loan_recovered_count else ""),
                        )
                        db.flush()
                        db.refresh(session)
                except InsufficientBalanceError:
                    raise AuctionError("结算划转失败，账本状态异常，结算已整体回滚")
                except ValueError as exc:
                    if isinstance(exc, AuctionError):
                        raise
                    raise AuctionError(f"结算联动履约核销失败，整笔结算已回滚：{exc}")
        return session


# --------------------------------------------------------------------------- #
# 已结算成交单的监管冲正与违约回退
# ---------------------------------------------------------------------------
#
# 冲正（reverse）：监管对已结算成交单（支持单笔/批量/部分数量）做异常回退——
#   1. 回退结算划转：自买方收回配额划付卖方（买方出库/卖方入库冲正流水）；
#   2. 回滚该笔成交触发的履约清缴：按成交单流水归属精确解除冻结、退还补缴，
#      履约记录（cleared/frozen/deficit/status）与配额状态同步回退；
#   3. 买方自由可用不足时只收回拿得回的部分，不足部分登记违约欠额（defaulted），
#      受影响卖方已由“先解除冻结/退还”的配额全额补位，不承担敞口；
#   4. 全部账户、流水、履约、冲正单、审计在同一事务提交，失败整体回滚。
#
# 与报告冲正的顺序无关性：报告冲正退还已清缴配额时，把成交单归属的到账补缴
#   按成交单逐笔写 auction_clear_refund；本链路计算可退余额同样以成交单归属
#   流水账（auction_deficit_clear − auction_clear_refund）为准。两条回退链路
#   共用同一本账，任意先后顺序下同一吨补缴最多退还一次，系统总配额守恒。
#
# 违约回退（default recovery）：买方事后补足配额（手动或后续场次结算到账自动
#   追偿），自由可用配额划付受影响卖方并解除违约；先清缴后追偿，绝不挪用履约配额。


def _trade_outstanding_default(trade: AuctionTrade) -> float:
    return round(_num(trade.defaulted_amount) - _num(trade.repaid_amount), 4)


def _sum_tx_amount(db: Session, account_id: int, tx_types: list[str], trade_id: int) -> float:
    """汇总某账户下归属指定成交单的流水金额（冲正回滚的精确归属依据）。"""
    from sqlalchemy import func

    total = (
        db.query(func.coalesce(func.sum(AllowanceTransaction.amount), 0))
        .filter(
            AllowanceTransaction.account_id == account_id,
            AllowanceTransaction.auction_trade_id == trade_id,
            AllowanceTransaction.tx_type.in_(tx_types),
        )
        .scalar()
    )
    return round(float(total or 0.0), 4)


def _clearance_rollback_for_qty(
    db: Session,
    buyer_account_id: int,
    trade: AuctionTrade,
    qty: float,
) -> tuple[float, float]:
    """计算本次回退 qty 吨应回滚的联动清缴：返回 (解除冻结 f, 退还补缴 c)。

    只有结算时到账配额触发的自由补缴（auction_deficit_clear，带成交单归属）
    随交易冲正回滚；冻结核销使用买方自有冻结配额，与交易取消无关，不回滚。
    可退余额按成交单归属流水账计算：补缴合计 − 已退还合计。报告冲正退还
    已清缴配额时同样按成交单写 auction_clear_refund，两条回退链路共用
    同一本账，先后顺序无关、同一吨补缴不会被重复退还。
    """
    cleared_total = _sum_tx_amount(db, buyer_account_id, ["auction_deficit_clear"], trade.id)
    refunded_total = _sum_tx_amount(db, buyer_account_id, ["auction_clear_refund"], trade.id)
    c_left = round(cleared_total - refunded_total, 4)
    c_rev = round(min(max(c_left, 0.0), qty), 4)
    return 0.0, c_rev


def _set_quota_status(db: Session, company_id: int, year: int, status: str) -> None:
    from app.models.allowance import Quota

    quota = (
        db.query(Quota)
        .filter(Quota.company_id == company_id, Quota.year == year)
        .first()
    )
    if quota:
        quota.status = status


def _rollback_buyer_compliance(
    db: Session,
    buyer_id: int,
    year: int,
    f_rev: float,
    c_rev: float,
) -> None:
    """联动清缴回滚后重算买方活跃履约记录（无活跃记录则跳过）。

    仅到账补缴 c 随交易冲正回退：``cleared -= c``，对应义务恢复为缺口；
    f（冻结核销）为买方自有配额履约，不随交易回滚，``frozen`` 不变。
    """
    from app.models.allowance import ComplianceRecord

    record = (
        db.query(ComplianceRecord)
        .filter(
            ComplianceRecord.company_id == buyer_id,
            ComplianceRecord.year == year,
            ComplianceRecord.is_active == 1,
        )
        .first()
    )
    if record is None or record.status == "reversed":
        # 报告已冲正归档：清缴配额已由报告冲正退还，不再二次回滚
        return

    undone = round(f_rev + c_rev, 4)
    if undone <= 0:
        # 无可回滚清缴（例如报告冲正已退还、履约记录已重建）：
        # 不得触碰当前活跃记录，避免污染重新批准后的履约结果
        return

    new_cleared = round(_num(record.cleared_amount) - undone, 4)
    new_frozen = round(max(_num(record.frozen_amount) - f_rev, 0.0), 4)
    emission = round(float(record.verified_emission), 4)
    new_deficit = round(emission - new_cleared, 4)

    db.execute(
        update(ComplianceRecord)
        .where(ComplianceRecord.id == record.id)
        .values(
            cleared_amount=new_cleared,
            frozen_amount=new_frozen,
            deficit=new_deficit,
            status="compliant" if emission <= 0 or new_deficit <= 0 else "deficit",
            cleared_at=None if new_cleared <= 0 else ComplianceRecord.cleared_at,
        )
        .execution_options(synchronize_session=False)
    )
    db.expire_all()

    if emission <= 0 or new_deficit <= 0:
        _set_quota_status(db, buyer_id, year, "cleared")
    else:
        _set_quota_status(db, buyer_id, year, "allocated")


def reverse_settled_trades(
    db: Session,
    session_id: int,
    operator: Operator | None,
    reason: str,
    *,
    trade_ids: list[int] | None = None,
    quantities: dict[int, float] | None = None,
    idempotency_key: str | None = None,
) -> AuctionReversalBatch:
    """监管冲正已结算成交单（整笔 / 指定笔 / 部分数量），返回冲正批次。

    - 仅 settled/defaulted 场次的已成交（含违约）成交单可冲正；reserved/cancelled 拒绝；
    - quantities={trade_id: qty} 部分回退；缺省整笔回退剩余未冲正量；
    - 同批次多笔成交与全部账户/流水/履约/审计在单一事务提交；
    - 幂等键唯一约束 + 场次键锁串行化，双击/并发重复冲正只生效一次；
    - 买方自由可用不足：收回可得部分，不足登记违约欠额（成交单 defaulted）。
    """
    reason = (reason or "").strip()
    if len(reason) < 2:
        raise AuctionError("请填写冲正原因（至少 2 个字符）")
    quantities = quantities or {}

    with locked_accounts([auction_session_key(session_id)]):
        session = _get_session(db, session_id)
        if session.status not in (SETTLED,):
            raise AuctionError(f"场次当前为 {session.status}，仅已结算场次可冲正成交单")

        if idempotency_key:
            existing = (
                db.query(AuctionReversalBatch)
                .filter(AuctionReversalBatch.idempotency_key == idempotency_key)
                .first()
            )
            if existing:
                return existing

        trades_q = (
            db.query(AuctionTrade)
            .filter(
                AuctionTrade.session_id == session_id,
                AuctionTrade.status.in_([TRADE_SETTLED, TRADE_DEFAULTED]),
            )
            .order_by(AuctionTrade.alloc_seq.asc(), AuctionTrade.id.asc())
        )
        if trade_ids:
            trades_q = trades_q.filter(AuctionTrade.id.in_(trade_ids))
        trades = trades_q.all()
        if trade_ids and len(trades) != len(set(trade_ids)):
            raise AuctionError("存在不可冲正的成交单（未结算/已作废/不属于本场次）")
        if not trades:
            raise AuctionError("没有可冲正的已结算成交单")

        # 校验回退量
        plans: list[tuple[AuctionTrade, float]] = []
        for trade in trades:
            already = round(_num(trade.reversed_quantity), 4)
            remaining = round(_num(trade.quantity) - already, 4)
            if remaining <= 1e-9:
                raise AuctionError(f"成交单 {trade.trade_no} 已全部冲正，不能重复冲正")
            qty = _round4(quantities.get(trade.id, remaining))
            if qty <= 0:
                raise AuctionError(f"成交单 {trade.trade_no} 冲正数量必须为正数")
            if qty > remaining + 1e-9:
                raise AuctionError(
                    f"成交单 {trade.trade_no} 待冲正量仅 {remaining:g} 吨，"
                    f"不能冲正 {qty:g} 吨"
                )
            plans.append((trade, qty))

        company_ids = {c for t, _ in plans for c in (t.seller_id, t.buyer_id)}
        accounts: dict[int, AllowanceAccount] = {}
        keys: list[str] = []
        for cid in company_ids:
            acc = _get_account(db, cid, session.year)
            accounts[cid] = acc
            keys.append(account_lock_key(acc.id))
            keys.append(company_clear_key(cid, session.year))

        with locked_accounts(sorted(set(keys))):
            try:
                with transactional(db):
                    batch = AuctionReversalBatch(
                        batch_no=_gen_batch_no(db),
                        session_id=session_id,
                        reason=reason[:500],
                        operator_id=operator.id if operator else None,
                        idempotency_key=idempotency_key,
                    )
                    db.add(batch)
                    db.flush()
                    # 缓存批次主键/编号：循环内反复 expire_all，访问过期对象属性
                    # 会触发懒刷新（并发失败事务中可能拿到已删除行而报错）。
                    batch_id = batch.id
                    batch_no = batch.batch_no

                    tx_date = _today()
                    total_reverse = total_recovered = total_default = 0.0

                    for trade_ref, qty in plans:
                        trade = db.get(AuctionTrade, trade_ref.id)
                        buyer_acc = lock_row_for_write(db, accounts[trade.buyer_id].id)
                        accounts[trade.buyer_id] = buyer_acc
                        seller_acc = lock_row_for_write(db, accounts[trade.seller_id].id)
                        accounts[trade.seller_id] = seller_acc
                        # expire_all 后重取，保证随后对 trade 的累计字段赋值进入工作单元
                        trade = db.get(AuctionTrade, trade.id)

                        # 1) 回滚联动清缴：仅退还结算时到账即补缴的自由配额 c
                        # （冻结核销用的是买方自有冻结配额，交易取消不影响该部分履约）。
                        f_rev, c_rev = _clearance_rollback_for_qty(
                            db, buyer_acc.id, trade, qty
                        )
                        buyer_name = _company_name(db, trade.buyer_id)
                        seller_name = _company_name(db, trade.seller_id)

                        if c_rev > 0:
                            bal, frz, rsv = apply_ledger_delta(db, buyer_acc.id, c_rev, 0, 0)
                            _add_ledger(
                                db, buyer_acc, "auction_clear_refund", c_rev,
                                bal, frz, rsv, "监管冲正",
                                f"成交单 {trade.trade_no} 冲正，退还已补缴缺口配额 {c_rev} 吨",
                                trade, tx_date,
                            )
                        _rollback_buyer_compliance(
                            db, trade.buyer_id, session.year, f_rev, c_rev
                        )

                        # 2) 自买方自由可用收回：冲正回退后其自由可用 = 结算前自由 + qty
                        # （解除冻结部分保留履约用途，不参与划付）；可用不足时只收回
                        # 拿得回的部分，缺口登记为买方违约欠额，卖方已由上面的
                        # 解冻/退还配额即时补位。
                        buyer_acc = db.get(AllowanceAccount, buyer_acc.id)
                        free = round(
                            float(buyer_acc.current_balance)
                            - float(buyer_acc.frozen_balance)
                            - float(buyer_acc.reserved_balance),
                            4,
                        )
                        recovered = round(min(qty, max(free, 0.0)), 4)
                        defaulted = round(qty - recovered, 4)

                        if recovered > 0:
                            b_bal, b_frz, b_rsv = apply_ledger_delta(
                                db, buyer_acc.id, -recovered, 0, 0
                            )
                            _add_ledger(
                                db, buyer_acc, "auction_clawback_out", recovered,
                                b_bal, b_frz, b_rsv, seller_name,
                                f"成交单 {trade.trade_no} 冲正，收回配额 {recovered} 吨"
                                + (f"，违约欠缴 {defaulted:g} 吨" if defaulted > 0 else ""),
                                trade, tx_date,
                            )
                            s_bal, s_frz, s_rsv = apply_ledger_delta(
                                db, seller_acc.id, recovered, 0, 0
                            )
                            _add_ledger(
                                db, seller_acc, "auction_clawback_in", recovered,
                                s_bal, s_frz, s_rsv, buyer_name,
                                f"成交单 {trade.trade_no} 监管冲正退回 {recovered} 吨",
                                trade, tx_date,
                            )

                        # 3) 回写成交单累计冲正量/违约欠额（条件 UPDATE，避免
                        # 循环内 expire_all 使 ORM 赋值丢失）
                        trade = db.get(AuctionTrade, trade.id)
                        new_reversed = round(_num(trade.reversed_quantity) + qty, 4)
                        new_defaulted = round(_num(trade.defaulted_amount) + defaulted, 4)
                        fully = new_reversed + 1e-9 >= _num(trade.quantity)
                        outstanding = round(new_defaulted - _num(trade.repaid_amount), 4)
                        if fully and outstanding <= 1e-9:
                            new_status = TRADE_REVERSED
                        elif defaulted > 0 or outstanding > 1e-9:
                            new_status = TRADE_DEFAULTED
                        else:
                            new_status = trade.status
                        db.execute(
                            update(AuctionTrade)
                            .where(AuctionTrade.id == trade.id)
                            .values(
                                reversed_quantity=new_reversed,
                                defaulted_amount=new_defaulted,
                                status=new_status,
                            )
                            .execution_options(synchronize_session=False)
                        )
                        trade.reversed_quantity = new_reversed
                        trade.defaulted_amount = new_defaulted
                        trade.status = new_status

                        db.add(AuctionTradeReversal(
                            reversal_no=_gen_reversal_no(db),
                            batch_id=batch_id,
                            session_id=session_id,
                            trade_id=trade.id,
                            buyer_id=trade.buyer_id,
                            seller_id=trade.seller_id,
                            year=session.year,
                            quantity=qty,
                            clear_unfrozen=f_rev,
                            clear_refunded=c_rev,
                            recovered_quantity=recovered,
                            defaulted_quantity=defaulted,
                            reason=reason[:500],
                        ))

                        write_audit(
                            db, operator, "trade.reverse",
                            target_type="trade", target_id=trade.id, session_id=session_id,
                            detail=f"冲正成交单 {trade.trade_no} {qty:g} 吨：解除冻结 {f_rev:g}、"
                            f"退还补缴 {c_rev:g}、收回 {recovered:g}"
                            + (f"、买方违约欠缴 {defaulted:g}" if defaulted > 0 else " 足额收回"),
                        )

                        total_reverse = round(total_reverse + qty, 4)
                        total_recovered = round(total_recovered + recovered, 4)
                        total_default = round(total_default + defaulted, 4)

                    # 循环内多次 expire_all，批次统计用条件 UPDATE 落库，避免
                    # ORM 属性赋值丢失
                    db.execute(
                        update(AuctionReversalBatch)
                        .where(AuctionReversalBatch.id == batch_id)
                        .values(
                            trade_count=len(plans),
                            reverse_volume=total_reverse,
                            recovered_volume=total_recovered,
                            default_volume=total_default,
                        )
                        .execution_options(synchronize_session=False)
                    )

                    write_audit(
                        db, operator, "session.reverse",
                        target_type="session", target_id=session_id, session_id=session_id,
                        detail=f"场次 {session.session_no} 监管冲正批次 {batch_no}："
                        f"{len(plans)} 笔 / 回退 {total_reverse:g} 吨，收回 {total_recovered:g} 吨"
                        + (f"，登记违约欠额 {total_default:g} 吨" if total_default > 0 else "，无违约"),
                    )
                    db.flush()
                    batch = db.get(AuctionReversalBatch, batch_id)
                    db.refresh(batch)
            except InsufficientBalanceError:
                raise AuctionError("冲正划转失败，账本状态异常，冲正已整体回滚")
            except IntegrityError as exc:
                if idempotency_key and is_duplicate_submit(exc):
                    db.rollback()
                    existing = (
                        db.query(AuctionReversalBatch)
                        .filter(AuctionReversalBatch.idempotency_key == idempotency_key)
                        .first()
                    )
                    if existing:
                        return existing
                raise AuctionError("冲正请求重复或并发冲突，请刷新后重试")
            return batch


def _defaulted_trades_for_buyer(db: Session, buyer_id: int, year: int) -> list[AuctionTrade]:
    return (
        db.query(AuctionTrade)
        .filter(
            AuctionTrade.buyer_id == buyer_id,
            AuctionTrade.year == year,
            AuctionTrade.defaulted_amount > AuctionTrade.repaid_amount,
        )
        .order_by(AuctionTrade.id.asc())
        .all()
    )


def _apply_one_repayment(
    db: Session,
    trade: AuctionTrade,
    amount: float,
    tx_date: str,
    *,
    source: str,
    operator: Operator | None,
    remark: str,
    idempotency_key: str | None = None,
) -> AuctionDefaultRepayment:
    """在已持锁的写事务内执行一笔违约追偿（买方出库 → 卖方入库）。"""
    buyer_acc = _get_account(db, trade.buyer_id, trade.year)
    seller_acc = _get_account(db, trade.seller_id, trade.year)
    buyer_acc = lock_row_for_write(db, buyer_acc.id)
    seller_acc = lock_row_for_write(db, seller_acc.id)

    bal, frz, rsv = apply_ledger_delta(db, buyer_acc.id, -amount, 0, 0)
    buyer_name = _company_name(db, trade.buyer_id)
    seller_name = _company_name(db, trade.seller_id)
    _add_ledger(
        db, buyer_acc, "auction_default_repay_out", amount,
        bal, frz, rsv, seller_name,
        f"违约补缴：成交单 {trade.trade_no} 欠额追偿划出 {amount:g} 吨",
        trade, tx_date,
    )
    s_bal, s_frz, s_rsv = apply_ledger_delta(db, seller_acc.id, amount, 0, 0)
    _add_ledger(
        db, seller_acc, "auction_default_repay_in", amount,
        s_bal, s_frz, s_rsv, buyer_name,
        f"违约补缴：成交单 {trade.trade_no} 欠额追偿到账 {amount:g} 吨",
        trade, tx_date,
    )

    new_repaid = round(_num(trade.repaid_amount) + amount, 4)
    fully = _num(trade.reversed_quantity) + 1e-9 >= _num(trade.quantity)
    outstanding_after = round(_num(trade.defaulted_amount) - new_repaid, 4)
    new_status = TRADE_REVERSED if (fully and outstanding_after <= 1e-9) else trade.status
    db.execute(
        update(AuctionTrade)
        .where(AuctionTrade.id == trade.id)
        .values(repaid_amount=new_repaid, status=new_status)
        .execution_options(synchronize_session=False)
    )
    trade.repaid_amount = new_repaid
    trade.status = new_status

    repayment = AuctionDefaultRepayment(
        repay_no=_gen_repay_no(db),
        session_id=trade.session_id,
        trade_id=trade.id,
        buyer_id=trade.buyer_id,
        seller_id=trade.seller_id,
        year=trade.year,
        quantity=amount,
        source=source,
        operator_id=operator.id if operator else None,
        remark=(remark or "")[:256],
        idempotency_key=idempotency_key,
    )
    db.add(repayment)
    db.flush()
    return repayment


def _recover_buyer_arrears(
    db: Session,
    buyer_id: int,
    year: int,
    tx_date: str,
    *,
    source: str,
    session: AuctionSession | None = None,
    operator: Operator | None = None,
    remark: str = "",
) -> dict:
    """用买方自由可用配额追偿其全部违约欠额（调用方已持锁并开启写事务）。

    先清缴后追偿：仅使用 current - frozen - reserved；逐笔按时间顺序偿还。
    返回 ``{"repayments": [...], "recovered": x}``；无欠额/无可用为空操作。
    """
    trades = [t for t in _defaulted_trades_for_buyer(db, buyer_id, year)]
    if not trades:
        return {"repayments": [], "recovered": 0.0}

    buyer_acc = lock_row_for_write(db, _get_account(db, buyer_id, year).id)
    free = round(
        float(buyer_acc.current_balance)
        - float(buyer_acc.frozen_balance)
        - float(buyer_acc.reserved_balance),
        4,
    )
    budget = max(free, 0.0)
    repayments: list[AuctionDefaultRepayment] = []
    recovered_total = 0.0

    for trade in trades:
        if budget <= 1e-9:
            break
        outstanding = _trade_outstanding_default(trade)
        if outstanding <= 1e-9:
            continue
        pay = round(min(outstanding, budget), 4)
        if pay <= 0:
            continue
        # 卖方账户行锁（账户键由外层统一持有，锁序 account < auction < clear）
        lock_row_for_write(db, _get_account(db, trade.seller_id, year).id)
        rmk = remark or (
            f"场次 {session.session_no} 结算到账自动追偿" if session else "违约欠额自动追偿"
        )
        repayment = _apply_one_repayment(
            db, trade, pay, tx_date, source=source, operator=operator, remark=rmk
        )
        write_audit(
            db, operator,
            "trade.default.auto_recover" if source == "auto" else "trade.default.recover",
            target_type="trade", target_id=trade.id, session_id=trade.session_id,
            detail=(f"买方 {_company_name(db, buyer_id)} 自动追偿成交单 {trade.trade_no} "
                    f"{pay:g} 吨") if source == "auto"
            else f"监管手动追偿成交单 {trade.trade_no} {pay:g} 吨",
        )
        repayments.append(repayment)
        budget = round(budget - pay, 4)
        recovered_total = round(recovered_total + pay, 4)

    return {"repayments": repayments, "recovered": recovered_total}


def recover_buyer_defaults(
    db: Session,
    buyer_id: int,
    year: int,
    operator: Operator | None,
) -> dict:
    """监管手动触发：对买方某年度全部违约欠额执行追偿（自由可用不足则尽力而为）。"""
    trades = _defaulted_trades_for_buyer(db, buyer_id, year)
    if not trades:
        raise AuctionError("该企业该年度没有待追偿的违约欠额")

    # 锁序与其他竞价写操作一致：先场次键（可能涉及多个历史场次），后账户/清缴键，
    # 避免与结算（先持有本场次键、再取历史场次键）形成锁环。
    session_keys = sorted({auction_session_key(t.session_id) for t in trades})
    account_keys = [
        account_lock_key(_get_account(db, buyer_id, year).id),
        company_clear_key(buyer_id, year),
    ]
    for t in trades:
        account_keys.append(account_lock_key(_get_account(db, t.seller_id, year).id))
    account_keys = sorted(set(account_keys))

    result = {"repayments": [], "recovered": 0.0}
    with locked_accounts(session_keys):
        with locked_accounts(account_keys):
            try:
                with transactional(db):
                    result = _recover_buyer_arrears(
                        db, buyer_id, year, _today(),
                        source="manual", operator=operator,
                        remark="监管手动触发违约追偿",
                    )
                    db.flush()
                    for r in result["repayments"]:
                        db.refresh(r)
            except InsufficientBalanceError:
                raise AuctionError("违约追偿划转失败，账本状态异常，操作已回滚")
            if not result["repayments"]:
                raise AuctionError("买方自由可用配额不足，暂无可追偿配额，请待其补足后重试")
            return result


def repay_trade_default(
    db: Session,
    trade_id: int,
    operator: Operator | None,
    *,
    amount: float | None = None,
    idempotency_key: str | None = None,
) -> AuctionDefaultRepayment:
    """监管对单笔违约成交单手动追偿；amount 缺省为全额，受买方自由可用约束。"""
    trade = db.get(AuctionTrade, trade_id)
    if trade is None:
        raise AuctionError("成交单不存在")

    with locked_accounts([auction_session_key(trade.session_id)]):
        trade = db.get(AuctionTrade, trade_id)
        outstanding = _trade_outstanding_default(trade)
        if outstanding <= 1e-9:
            raise AuctionError("成交单不存在待追偿的违约欠额")

        if idempotency_key:
            existing = (
                db.query(AuctionDefaultRepayment)
                .filter(AuctionDefaultRepayment.idempotency_key == idempotency_key)
                .first()
            )
            if existing:
                return existing

        keys = [
            account_lock_key(_get_account(db, trade.buyer_id, trade.year).id),
            account_lock_key(_get_account(db, trade.seller_id, trade.year).id),
            company_clear_key(trade.buyer_id, trade.year),
        ]
        with locked_accounts(sorted(set(keys))):
            try:
                with transactional(db):
                    buyer_acc = lock_row_for_write(
                        db, _get_account(db, trade.buyer_id, trade.year).id
                    )
                    free = round(
                        float(buyer_acc.current_balance)
                        - float(buyer_acc.frozen_balance)
                        - float(buyer_acc.reserved_balance),
                        4,
                    )
                    wanted = outstanding if amount is None else _round4(amount)
                    if wanted <= 0:
                        raise AuctionError("追偿数量必须为正数")
                    pay = round(min(wanted, outstanding, max(free, 0.0)), 4)
                    if pay <= 0:
                        raise AuctionError(
                            "买方自由可用配额不足，暂无法追偿，请待其补足配额后重试"
                        )
                    repayment = _apply_one_repayment(
                        db, trade, pay, _today(),
                        source="manual", operator=operator,
                        remark="监管手动追偿违约欠额",
                        idempotency_key=idempotency_key,
                    )
                    write_audit(
                        db, operator, "trade.default.recover",
                        target_type="trade", target_id=trade_id,
                        session_id=trade.session_id,
                        detail=f"监管追偿成交单 {trade.trade_no} {pay:g} 吨，"
                        f"剩余欠额 {_trade_outstanding_default(trade):g} 吨",
                    )
                    db.flush()
                    db.refresh(repayment)
            except InsufficientBalanceError:
                raise AuctionError("违约追偿划转失败，账本状态异常，操作已回滚")
            except IntegrityError as exc:
                if idempotency_key and is_duplicate_submit(exc):
                    db.rollback()
                    existing = (
                        db.query(AuctionDefaultRepayment)
                        .filter(AuctionDefaultRepayment.idempotency_key == idempotency_key)
                        .first()
                    )
                    if existing:
                        return existing
                raise
            return repayment


# --------------------------------------------------------------------------- #
# 查询辅助
# --------------------------------------------------------------------------- #

def list_sessions(db: Session, *, year: int | None = None, status: str | None = None) -> list[AuctionSession]:
    q = db.query(AuctionSession)
    if year is not None:
        q = q.filter(AuctionSession.year == year)
    if status:
        q = q.filter(AuctionSession.status == status)
    return q.order_by(AuctionSession.id.desc()).all()


def list_bids(
    db: Session,
    *,
    session_id: int | None = None,
    company_id: int | None = None,
    status: str | None = None,
) -> list[AuctionBid]:
    q = db.query(AuctionBid)
    if session_id is not None:
        q = q.filter(AuctionBid.session_id == session_id)
    if company_id is not None:
        q = q.filter(AuctionBid.company_id == company_id)
    if status:
        q = q.filter(AuctionBid.status == status)
    return q.order_by(AuctionBid.id.asc()).all()


def list_trades(
    db: Session,
    *,
    session_id: int | None = None,
    company_id: int | None = None,
) -> list[AuctionTrade]:
    q = db.query(AuctionTrade)
    if session_id is not None:
        q = q.filter(AuctionTrade.session_id == session_id)
    if company_id is not None:
        q = q.filter(
            (AuctionTrade.buyer_id == company_id) | (AuctionTrade.seller_id == company_id)
        )
    return q.order_by(AuctionTrade.alloc_seq.asc(), AuctionTrade.id.asc()).all()


def list_audit_logs(
    db: Session,
    *,
    session_id: int | None = None,
    limit: int = 200,
) -> list[AuctionAuditLog]:
    q = db.query(AuctionAuditLog)
    if session_id is not None:
        q = q.filter(AuctionAuditLog.session_id == session_id)
    return q.order_by(AuctionAuditLog.id.desc()).limit(limit).all()


def list_reversal_batches(
    db: Session,
    *,
    session_id: int | None = None,
) -> list[AuctionReversalBatch]:
    q = db.query(AuctionReversalBatch)
    if session_id is not None:
        q = q.filter(AuctionReversalBatch.session_id == session_id)
    return q.order_by(AuctionReversalBatch.id.desc()).all()


def list_trade_reversals(
    db: Session,
    *,
    session_id: int | None = None,
    trade_id: int | None = None,
) -> list[AuctionTradeReversal]:
    q = db.query(AuctionTradeReversal)
    if session_id is not None:
        q = q.filter(AuctionTradeReversal.session_id == session_id)
    if trade_id is not None:
        q = q.filter(AuctionTradeReversal.trade_id == trade_id)
    return q.order_by(AuctionTradeReversal.id.asc()).all()


def list_default_repayments(
    db: Session,
    *,
    session_id: int | None = None,
    trade_id: int | None = None,
    buyer_id: int | None = None,
) -> list[AuctionDefaultRepayment]:
    q = db.query(AuctionDefaultRepayment)
    if session_id is not None:
        q = q.filter(AuctionDefaultRepayment.session_id == session_id)
    if trade_id is not None:
        q = q.filter(AuctionDefaultRepayment.trade_id == trade_id)
    if buyer_id is not None:
        q = q.filter(AuctionDefaultRepayment.buyer_id == buyer_id)
    return q.order_by(AuctionDefaultRepayment.id.asc()).all()


def list_defaulted_trades(
    db: Session,
    *,
    buyer_id: int | None = None,
    year: int | None = None,
) -> list[AuctionTrade]:
    q = db.query(AuctionTrade).filter(
        AuctionTrade.defaulted_amount > AuctionTrade.repaid_amount
    )
    if buyer_id is not None:
        q = q.filter(AuctionTrade.buyer_id == buyer_id)
    if year is not None:
        q = q.filter(AuctionTrade.year == year)
    return q.order_by(AuctionTrade.id.asc()).all()
