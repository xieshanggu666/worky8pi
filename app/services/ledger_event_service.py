"""统一账本事件登记服务：业务事务内实时追加事件，并从旧业务表幂等回填。

两类入口：

- :func:`record_event_for_tx`：挂在四个流水写入点（台账划转 / 配额内核 /
  企业间订单 / 集中竞价）的同一事务内，流水与事件同生共死；
- :func:`backfill_ledger_events`：迁移脚本与初始化调用，扫描五类业务表，
  把旧流水与业务单据状态补登为 ``is_legacy=1`` 的历史事件。

事件只追加（append-only），``(source, source_ref, occurrence)`` 唯一键保证
双击、超时重试、并发回填下同一业务事实只登记一次；全局 ``seq`` 在进程锁 +
事务内分配，唯一约束兜底，冲突时重新取号重试。
"""

from __future__ import annotations

import hashlib
import json
import threading
from datetime import datetime
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.event_semantics import semantics_for
from app.models.allowance import (
    AllowanceAccount,
    AllowanceTransaction,
    ComplianceRecord,
    TradeOrder,
)
from app.models.auction import (
    AuctionBid,
    AuctionDefaultRepayment,
    AuctionSession,
    AuctionTrade,
    AuctionTradeReversal,
)
from app.models.ledger import LedgerEvent
from app.models.report import MrvReport

# 全局序号分配锁：与业务账户键锁不同，它只保护“取号→插入”的极短临界区
_seq_guard = threading.RLock()

# 流水类型 -> 业务来源
_TX_SOURCE = {
    "allocation": "quota_allocation",
    "buy": "manual_transfer",
    "sell": "manual_transfer",
    "transfer_in": "manual_transfer",
    "transfer_out": "manual_transfer",
    "freeze": "compliance",
    "clear": "compliance",
    "frozen_clear": "compliance",
    "trade_deficit_clear": "trade_order",
    "reversal": "report_reversal",
    "reversal_unfreeze": "report_reversal",
}
_AUCTION_TX_SOURCE = {
    "auction_bid_reserve": "auction_bid",
    "auction_bid_release": "auction_bid",
    "auction_reserve_release": "auction_session",
    "auction_deliver_out": "auction_trade",
    "auction_deliver_in": "auction_trade",
    "auction_deficit_clear": "auction_trade",
    "auction_clear_refund": "auction_reversal",
    "auction_clear_unfreeze": "auction_reversal",
    "auction_clawback_out": "auction_reversal",
    "auction_clawback_in": "auction_reversal",
    "auction_default_repay_out": "auction_repay",
    "auction_default_repay_in": "auction_repay",
}


def _canonical(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def compute_chain_hash(
    *,
    seq: int,
    source: str,
    source_ref: int | None,
    occurrence: int,
    account_id: int | None,
    event_type: str,
    amount: float,
    event_group: str,
    prev_hash: str,
) -> str:
    """计算事件链哈希：任何内容篡改/断序都会在重放校验时暴露。"""
    body = _canonical(
        {
            "seq": seq,
            "source": source,
            "source_ref": source_ref,
            "occurrence": occurrence,
            "account_id": account_id,
            "event_type": event_type,
            "amount": round(float(amount), 4),
            "event_group": event_group,
            "prev": prev_hash,
        }
    )
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _max_seq(db: Session) -> int:
    value = db.query(func.coalesce(func.max(LedgerEvent.seq), 0)).scalar()
    return int(value or 0)


def _last_hash(db: Session, seq: int) -> str:
    if seq <= 0:
        return ""
    row = db.query(LedgerEvent.chain_hash).filter(LedgerEvent.seq == seq).first()
    return str(row[0]) if row else ""


def _insert_events(db: Session, events: list[LedgerEvent]) -> int:
    """分配连续 seq 与链式哈希，并把事件加入会话（不在此显式 flush）。

    设计要点：
    - 本函数可能在 ``after_flush`` 钩子内被调用，此时显式 flush/开 savepoint 都会
      重入 flush 状态机而报错；因此只“暂存”（session.add），由 SQLAlchemy 在
      **同一轮 flush** 内把新加入的对象一并落库（versioned-history 同款机制），
      事件与触发它的流水同事务提交；
    - 非钩子路径（如回填）调用后由调用方 commit，行为一致；
    - 同会话同一轮 flush 内可能暂存多批事件，而 MAX(seq) 只统计已落库行，故用
      ``session.info`` 记录本会话已分配到的最大 seq 做续号，避免同轮内撞号；
    - 跨进程并发由进程锁 + ``uq_ledger_event_seq`` 唯一约束双保险，极端撞号时
      业务事务整体失败，调用方按既有幂等键重试即可，不会留下半条链。
    """
    if not events:
        return 0
    info = db.info.setdefault("ledger_event_seq", {"max": None})
    with _seq_guard:
        if info["max"] is None:
            info["max"] = _max_seq(db)
        prev_hash = _last_hash(db, info["max"])
        for event in events:
            next_seq = info["max"] + 1
            event.seq = next_seq
            event.prev_hash = prev_hash
            event.chain_hash = compute_chain_hash(
                seq=event.seq,
                source=event.source,
                source_ref=event.source_ref,
                occurrence=event.occurrence,
                account_id=event.account_id,
                event_type=event.event_type,
                amount=float(event.amount or 0),
                event_group=event.event_group or "",
                prev_hash=prev_hash,
            )
            prev_hash = event.chain_hash
            info["max"] = next_seq
            db.add(event)
    return len(events)


def _event_exists(db: Session, source: str, source_ref: int | None, occurrence: int = 0) -> bool:
    return (
        db.query(LedgerEvent.id)
        .filter(
            LedgerEvent.source == source,
            LedgerEvent.source_ref == source_ref,
            LedgerEvent.occurrence == occurrence,
        )
        .first()
        is not None
    )


def classify_tx_source(tx: AllowanceTransaction) -> tuple[str, str]:
    """根据流水类型/关联单据返回 ``(业务来源, 事件组)``。"""
    tx_type = tx.tx_type
    if tx.auction_trade_id is not None:
        source = _AUCTION_TX_SOURCE.get(tx_type, "auction_trade")
        return source, f"auction_trade:{tx.auction_trade_id}"
    if tx.trade_order_id is not None:
        return "trade_order", f"trade_order:{tx.trade_order_id}"
    source = _TX_SOURCE.get(tx_type)
    if source is not None:
        if source == "trade_order":
            return source, f"trade_order:{tx.trade_order_id}"
        return source, f"{source}:{tx.id}"
    if tx_type.startswith("auction_"):
        return _AUCTION_TX_SOURCE.get(tx_type, "auction_trade"), f"auction_tx:{tx.id}"
    return "manual_transfer", f"manual_transfer:{tx.id}"


def record_event_for_tx(
    db: Session,
    tx: AllowanceTransaction,
    *,
    is_legacy: bool = False,
) -> LedgerEvent | None:
    """在流水所在事务内追加对应账本事件。

    必须在流水 flush（拿到主键）之后调用——本钩子挂在 after_flush，正是该时点。
    事件与流水同一事务提交：流水成功而事件缺失（或反之）的半成品状态不可能落库。
    已存在同来源事件（回填/重试）时幂等跳过。
    """
    if tx.id is None:
        return None
    source, group = classify_tx_source(tx)
    if _event_exists(db, source, tx.id, 0):
        return None

    sem = semantics_for(tx.tx_type)
    payload = _canonical(
        {
            "amount": round(float(tx.amount or 0), 4),
            "balance_after": round(float(tx.balance_after or 0), 4),
            "frozen_after": round(float(tx.frozen_after or 0), 4),
            "reserved_after": round(float(tx.reserved_after or 0), 4),
            "counterparty": tx.counterparty or "",
            "tx_date": tx.tx_date or "",
            "remark": tx.remark or "",
            "domain": sem.domain,
        }
    )
    event = LedgerEvent(
        event_group=group,
        source=source,
        source_ref=tx.id,
        occurrence=0,
        is_legacy=1 if is_legacy else 0,
        account_id=tx.account_id,
        company_id=tx.company_id,
        year=_tx_year(db, tx),
        event_type=tx.tx_type,
        direction=sem.direction,
        amount=round(float(tx.amount or 0), 4),
        trade_order_id=tx.trade_order_id,
        auction_trade_id=tx.auction_trade_id,
        idempotency_key=tx.idempotency_key,
        payload=payload,
        created_at=tx.created_at or datetime.utcnow(),
        occurred_at=tx.created_at or datetime.utcnow(),
    )
    _insert_events(db, [event])
    return event


def _tx_year(db: Session, tx: AllowanceTransaction) -> int | None:
    """流水本身不记年度：经账户取得（账户按企业+年度开立）。"""
    account = db.get(AllowanceAccount, tx.account_id)
    return account.year if account else None


# --------------------------------------------------------------------------- #
# 业务单据状态事件（document 视角）：让订单/竞价/履约/报告的生命周期也进入同一条链
# --------------------------------------------------------------------------- #

def _status_event(
    *,
    source: str,
    source_ref: int,
    occurrence: int,
    event_type: str,
    company_id: int | None,
    year: int | None,
    group: str,
    status: str,
    occurred_at: datetime | None,
    extra: dict[str, Any] | None = None,
) -> LedgerEvent:
    payload = {"status": status}
    if extra:
        payload.update(extra)
    return LedgerEvent(
        event_group=group,
        source=source,
        source_ref=source_ref,
        occurrence=occurrence,
        is_legacy=1,
        account_id=None,
        company_id=company_id,
        year=year,
        event_type=event_type,
        direction="status",
        amount=0,
        payload=_canonical(_json_safe(payload)),
        occurred_at=occurred_at or datetime.utcnow(),
    )


def _json_safe(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def _status_events_for_documents(db: Session) -> list[LedgerEvent]:
    events: list[LedgerEvent] = []

    def add(ev: LedgerEvent) -> None:
        if not _event_exists(db, ev.source, ev.source_ref, ev.occurrence):
            events.append(ev)

    # 企业间订单：确认/撤销/交割三个可观察时点
    for order in db.query(TradeOrder).all():
        group = f"trade_order:{order.id}"
        if order.confirmed_at:
            add(_status_event(
                source="trade_order", source_ref=order.id, occurrence=10,
                event_type="order_status", company_id=order.seller_id, year=order.year,
                group=group, status="confirmed", occurred_at=order.confirmed_at,
                extra={"order_no": order.order_no, "amount": float(order.amount or 0)},
            ))
        if order.delivered_at:
            add(_status_event(
                source="trade_order", source_ref=order.id, occurrence=20,
                event_type="order_status", company_id=order.buyer_id, year=order.year,
                group=group, status="delivered", occurred_at=order.delivered_at,
                extra={"order_no": order.order_no},
            ))
        if order.cancelled_at:
            add(_status_event(
                source="trade_order", source_ref=order.id, occurrence=20,
                event_type="order_status", company_id=order.cancelled_by, year=order.year,
                group=group, status="cancelled", occurred_at=order.cancelled_at,
                extra={"order_no": order.order_no, "reason": order.cancel_reason},
            ))

    # 竞价场次状态机
    for s in db.query(AuctionSession).all():
        group = f"auction_session:{s.id}"
        for occ, status, when in (
            (10, "open", s.open_at),
            (20, "matched", s.matched_at),
            (30, "settled", s.settled_at),
            (40, "cancelled", s.cancelled_at),
        ):
            if when:
                add(_status_event(
                    source="auction_session", source_ref=s.id, occurrence=occ,
                    event_type="auction_session_status", company_id=None, year=s.year,
                    group=group, status=status, occurred_at=when,
                    extra={"session_no": s.session_no},
                ))

    # 报价单终态
    for bid in db.query(AuctionBid).all():
        when = bid.matched_at or bid.cancelled_at
        if bid.status in ("active",):
            continue
        add(_status_event(
            source="auction_bid", source_ref=bid.id, occurrence=10,
            event_type="auction_bid_status", company_id=bid.company_id, year=bid.year,
            group=f"auction_bid:{bid.id}", status=bid.status, occurred_at=when,
            extra={"bid_no": bid.bid_no, "side": bid.side,
                   "filled": float(bid.filled_quantity or 0)},
        ))

    # 成交单：结算 / 冲正 / 违约
    for trade in db.query(AuctionTrade).all():
        group = f"auction_trade:{trade.id}"
        if trade.settled_at:
            add(_status_event(
                source="auction_trade", source_ref=trade.id, occurrence=10,
                event_type="auction_trade_status", company_id=trade.buyer_id,
                year=trade.year, group=group, status="settled",
                occurred_at=trade.settled_at, extra={"trade_no": trade.trade_no},
            ))
        if trade.cancelled_at:
            add(_status_event(
                source="auction_trade", source_ref=trade.id, occurrence=20,
                event_type="auction_trade_status", company_id=trade.seller_id,
                year=trade.year, group=group, status="cancelled",
                occurred_at=trade.cancelled_at, extra={"trade_no": trade.trade_no},
            ))
        if float(trade.reversed_quantity or 0) > 0:
            add(_status_event(
                source="auction_trade", source_ref=trade.id, occurrence=30,
                event_type="auction_trade_status", company_id=trade.buyer_id,
                year=trade.year, group=group, status=trade.status,
                occurred_at=trade.settled_at,
                extra={"trade_no": trade.trade_no,
                       "reversed": float(trade.reversed_quantity or 0),
                       "defaulted": float(trade.defaulted_amount or 0),
                       "repaid": float(trade.repaid_amount or 0)},
            ))

    # 履约记录（含冲正归档）与 MRV 报告状态
    for rec in db.query(ComplianceRecord).all():
        add(_status_event(
            source="compliance", source_ref=rec.id, occurrence=10,
            event_type="compliance_status", company_id=rec.company_id, year=rec.year,
            group=f"compliance:{rec.id}",
            status="archived" if not rec.is_active else rec.status,
            occurred_at=rec.cleared_at,
            extra={"cleared": float(rec.cleared_amount or 0),
                   "frozen": float(rec.frozen_amount or 0),
                   "deficit": float(rec.deficit or 0)},
        ))
    for report in db.query(MrvReport).all():
        when = report.approved_at or report.reversed_at or report.generated_at
        add(_status_event(
            source="mrv_report", source_ref=report.id, occurrence=10,
            event_type="report_status", company_id=report.company_id, year=report.year,
            group=f"mrv_report:{report.id}", status=report.status, occurred_at=when,
            extra={"emission": float(report.total_emission or 0)},
        ))

    return events


def backfill_ledger_events(db: Session, *, commit: bool = True) -> dict[str, int]:
    """扫描五类旧业务表，把缺失事件幂等补登为历史事件（可重复执行）。

    - 流水事件：按 ``allowance_transactions`` 逐笔补登，携带原始三余额快照；
    - 状态事件：订单/场次/报价/成交单/履约/报告的生命周期时点；
    - 冲正批次与违约追偿单也补登摘要事件，保证“冲正回退”整链可追溯。

    与实时登记共用 ``(source, source_ref, occurrence)`` 唯一键，因此在一个
    已经通过钩子实时记账的库上执行同样安全：只补缺、不重复。
    """
    # 账户年度缓存，避免逐笔流水查库
    year_map = {
        a.id: a.year
        for a in db.query(AllowanceAccount.id, AllowanceAccount.year).all()
    }
    tx_events: list[LedgerEvent] = []
    existing_keys = {
        (r[0], r[1], r[2])
        for r in db.query(LedgerEvent.source, LedgerEvent.source_ref, LedgerEvent.occurrence).all()
    }

    for tx in db.query(AllowanceTransaction).order_by(AllowanceTransaction.id.asc()).all():
        source, group = classify_tx_source(tx)
        if (source, tx.id, 0) in existing_keys:
            continue
        sem = semantics_for(tx.tx_type)
        payload = _canonical({
            "amount": round(float(tx.amount or 0), 4),
            "balance_after": round(float(tx.balance_after or 0), 4),
            "frozen_after": round(float(tx.frozen_after or 0), 4),
            "reserved_after": round(float(tx.reserved_after or 0), 4),
            "counterparty": tx.counterparty or "",
            "tx_date": tx.tx_date or "",
            "remark": tx.remark or "",
            "domain": sem.domain,
            "backfill": True,
        })
        tx_events.append(LedgerEvent(
            event_group=group,
            source=source,
            source_ref=tx.id,
            occurrence=0,
            is_legacy=1,
            account_id=tx.account_id,
            company_id=tx.company_id,
            year=year_map.get(tx.account_id),
            event_type=tx.tx_type,
            direction=sem.direction,
            amount=round(float(tx.amount or 0), 4),
            trade_order_id=tx.trade_order_id,
            auction_trade_id=tx.auction_trade_id,
            idempotency_key=tx.idempotency_key,
            payload=payload,
            created_at=tx.created_at or datetime.utcnow(),
            occurred_at=tx.created_at or datetime.utcnow(),
        ))

    doc_events = _status_events_for_documents(db)

    # 冲正批次/明细/追偿：以批次为组补登摘要，支撑“部分冲正”的逐笔追溯
    reversal_events: list[LedgerEvent] = []
    for rev in db.query(AuctionTradeReversal).order_by(AuctionTradeReversal.id.asc()).all():
        if ("auction_reversal_item", rev.id, 0) in existing_keys:
            continue
        reversal_events.append(LedgerEvent(
            event_group=f"auction_reversal_batch:{rev.batch_id}",
            source="auction_reversal_item",
            source_ref=rev.id,
            occurrence=0,
            is_legacy=1,
            account_id=None,
            company_id=rev.buyer_id,
            year=rev.year,
            event_type="auction_trade_reversal",
            direction="status",
            amount=round(float(rev.quantity or 0), 4),
            auction_trade_id=rev.trade_id,
            payload=_canonical({
                "quantity": float(rev.quantity or 0),
                "clear_refunded": float(rev.clear_refunded or 0),
                "recovered": float(rev.recovered_quantity or 0),
                "defaulted": float(rev.defaulted_quantity or 0),
            }),
            occurred_at=rev.created_at or datetime.utcnow(),
        ))
    for repay in db.query(AuctionDefaultRepayment).order_by(AuctionDefaultRepayment.id.asc()).all():
        if ("auction_repay_doc", repay.id, 0) in existing_keys:
            continue
        reversal_events.append(LedgerEvent(
            event_group=f"auction_repay:{repay.trade_id}",
            source="auction_repay_doc",
            source_ref=repay.id,
            occurrence=0,
            is_legacy=1,
            account_id=None,
            company_id=repay.buyer_id,
            year=repay.year,
            event_type="auction_default_repayment",
            direction="status",
            amount=round(float(repay.quantity or 0), 4),
            auction_trade_id=repay.trade_id,
            payload=_canonical({"source": repay.source, "quantity": float(repay.quantity or 0)}),
            occurred_at=repay.created_at or datetime.utcnow(),
        ))

    # 取号插入按 occurred_at/id 稳定排序，使历史链时间有序
    ordered = sorted(
        tx_events + doc_events + reversal_events,
        key=lambda e: (e.occurred_at or datetime.min, e.source or "", e.source_ref or 0, e.occurrence),
    )
    _insert_events(db, ordered)

    # 回填后直接重建一次全部检查点，投影与链同步到位
    from app.services.replay_service import rebuild_checkpoints

    rebuild_checkpoints(db, commit=False)

    if commit:
        db.commit()
    return {
        "tx_events": len(tx_events),
        "status_events": len(doc_events),
        "reversal_events": len(reversal_events),
        "total": len(ordered),
    }
