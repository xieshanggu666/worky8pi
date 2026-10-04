"""统一对账服务：事件链完整性、重放投影、流水勾稽、单据↔账本、履约与系统守恒。

对账把五类业务（配额流水 / 企业订单 / 集中竞价 / 履约清缴 / 冲正回退）拉到
同一本事件账上做端到端核对，输出结构化差异（code/severity/message/refs），
并把每次运行持久化为 ``ledger_reconciliations``（可追溯、幂等重跑）。

核对维度：

1. ``chain``       事件链：seq 连续无重复、prev_hash/chain_hash 勾连、内容哈希可重算；
2. ``projection``  重放投影：事件全量重放余额 == 账户实际余额；账本不变量
                    （current ≥ frozen + reserved，三项非负）；
3. ``flow``        流水勾稽：期初 + 流水有符号合计 == 当前余额；逐笔三余额快照链；
4. ``document``    单据↔账本：订单/竞价占用必释放或出库、交割配对、冲正累计≤成交量、
                    违约欠额=追偿余额、成交单归属补缴不超额退还；
5. ``compliance``  履约一致：活跃记录 cleared/frozen 与流水一致、报告批准↔履约记录、
                    配额状态可由履约状态推导；
6. ``conservation``系统守恒：全部跨主体内部划转（订单交割/竞价结算/冲正收回/违约追偿）
                    出入账两两相等；系统总配额 = 分配 − 净清缴 ± 外部市场买卖。

跨年度：账户按 (企业, 年度) 开立，所有余额事件强制带年度，逐 (企业, 年度) 独立
重放核对，年度间不串账；订单/成交单年度必须与其发生账户年度一致。
"""

from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.event_semantics import SEMANTICS, amount_signed_for_balance, semantics_for
from app.models.allowance import (
    AllowanceAccount,
    AllowanceTransaction,
    ComplianceRecord,
    Quota,
    TradeOrder,
)
from app.models.auction import (
    AuctionDefaultRepayment,
    AuctionSession,
    AuctionTrade,
    AuctionTradeReversal,
)
from app.models.ledger import LedgerCheckpoint, LedgerEvent, LedgerReconciliation
from app.models.report import MrvReport
from app.services.ledger_event_service import compute_chain_hash
from app.services.replay_service import replay_all

_EPS = 1e-6

# 对账运行串行化键（进程内），配合幂等键唯一约束兜底并发重试
_recon_guard = threading.RLock()

# 对持仓 current 有影响的全部流水类型（有符号方向取自语义注册表）
_BALANCE_TX_TYPES = [
    name for name, sem in SEMANTICS.items() if sem.vector[0] != 0
]

# 跨主体内部配对类型（出/入必须全局相等，系统守恒的核心）
_PAIRS: dict[str, tuple[str, str]] = {
    # 归属键名 -> (out 类型, in 类型)
    "trade_deliver": ("trade_deliver_out", "trade_deliver_in"),
    "auction_deliver": ("auction_deliver_out", "auction_deliver_in"),
    "auction_clawback": ("auction_clawback_out", "auction_clawback_in"),
    "auction_default_repay": ("auction_default_repay_out", "auction_default_repay_in"),
}

# 清缴出库（离仓）与冲正退还（回仓）类型
_CLEAR_OUT_TYPES = ("clear", "frozen_clear", "trade_deficit_clear", "auction_deficit_clear")
_CLEAR_REFUND_TYPES = ("reversal", "auction_clear_refund")


def _gen_recon_no(db: Session) -> str:
    return f"RC{datetime.utcnow():%Y%m%d%H%M%S}{uuid.uuid4().hex[:6].upper()}"


def _issue(issues: list[dict], code: str, severity: str, message: str, **refs: Any) -> None:
    issues.append({
        "code": code,
        "severity": severity,  # error / warning
        "message": message,
        "refs": {k: v for k, v in refs.items() if v is not None},
    })


def _near(a: float, b: float, eps: float = _EPS) -> bool:
    return abs(float(a) - float(b)) <= eps


# --------------------------------------------------------------------------- #
# 1. 事件链完整性
# --------------------------------------------------------------------------- #

def check_chain(db: Session, issues: list[dict]) -> int:
    """校验事件链：seq 从 1 连续、prev_hash 勾连、内容哈希可重算。返回事件数。"""
    events = db.query(LedgerEvent).order_by(LedgerEvent.seq.asc(), LedgerEvent.id.asc()).all()
    prev_hash = ""
    for expected_seq, event in enumerate(events, start=1):
        if event.seq != expected_seq:
            _issue(
                issues, "CHAIN_SEQ_GAP", "error",
                f"事件链序号在 {event.seq} 处断序（期望 {expected_seq}），疑似事件被物理删除或漏登",
                event_id=event.id, seq=event.seq, expected=expected_seq,
            )
            # 断序后继续比对相邻勾连，但不再要求从 1 连续
        if event.prev_hash != prev_hash:
            _issue(
                issues, "CHAIN_PREV_HASH", "error",
                f"事件 {event.seq} 的 prev_hash 与前一事件不衔接，链被断裂或重排",
                event_id=event.id, seq=event.seq, source=event.source,
                source_ref=event.source_ref,
            )
        recomputed = compute_chain_hash(
            seq=event.seq,
            source=event.source,
            source_ref=event.source_ref,
            occurrence=event.occurrence,
            account_id=event.account_id,
            event_type=event.event_type,
            amount=float(event.amount or 0),
            event_group=event.event_group or "",
            prev_hash=event.prev_hash,
        )
        if recomputed != event.chain_hash:
            _issue(
                issues, "CHAIN_CONTENT_TAMPERED", "error",
                f"事件 {event.seq} 内容哈希不一致，事件载荷被篡改或链损坏",
                event_id=event.id, seq=event.seq, source=event.source,
                source_ref=event.source_ref,
            )
        prev_hash = event.chain_hash
    return len(events)


# --------------------------------------------------------------------------- #
# 2. 重放投影 vs 账户实际余额
# --------------------------------------------------------------------------- #

def check_projections(db: Session, issues: list[dict], states: dict, *,
                      company_id: int | None, year: int | None) -> tuple[int, int]:
    accounts_q = db.query(AllowanceAccount)
    if company_id is not None:
        accounts_q = accounts_q.filter(AllowanceAccount.company_id == company_id)
    if year is not None:
        accounts_q = accounts_q.filter(AllowanceAccount.year == year)
    accounts = accounts_q.all()

    checked = 0
    for account in accounts:
        state = states.get(account.id)
        checked += 1
        actual_cur = float(account.current_balance)
        actual_frz = float(account.frozen_balance)
        actual_rsv = float(account.reserved_balance)

        # 账本不变量（即使没有事件也要成立）
        if actual_cur < -_EPS or actual_frz < -_EPS or actual_rsv < -_EPS:
            _issue(issues, "ACCOUNT_NEGATIVE", "error",
                   f"账户 {account.id}（企业{account.company_id}/{account.year}年度）出现负余额",
                   account_id=account.id, company_id=account.company_id, year=account.year,
                   current=actual_cur, frozen=actual_frz, reserved=actual_rsv)
        if actual_cur + _EPS < actual_frz + actual_rsv:
            _issue(issues, "ACCOUNT_FREE_RESERVED_OVERDRAFT", "error",
                   f"账户 {account.id} 持仓小于冻结+占用，履约/交易隔离不变量被破坏",
                   account_id=account.id, current=actual_cur,
                   frozen=actual_frz, reserved=actual_rsv)

        if state is None:
            if not (_near(actual_cur, 0) and _near(actual_frz, 0) and _near(actual_rsv, 0)):
                _issue(issues, "PROJECTION_NO_EVENTS", "error",
                       f"账户 {account.id} 有余额但事件流中无任何余额事件（旧数据未回填或账实不符）",
                       account_id=account.id, current=actual_cur)
            continue

        if not _near(state.current, actual_cur):
            _issue(issues, "PROJECTION_CURRENT_MISMATCH", "error",
                   f"账户 {account.id} 持仓重放值 {state.current:.4f} 与实际 {actual_cur:.4f} 不一致",
                   account_id=account.id, company_id=account.company_id, year=account.year,
                   replayed=state.current, actual=actual_cur)
        if not _near(state.frozen, actual_frz):
            _issue(issues, "PROJECTION_FROZEN_MISMATCH", "error",
                   f"账户 {account.id} 冻结额重放值 {state.frozen:.4f} 与实际 {actual_frz:.4f} 不一致",
                   account_id=account.id, replayed=state.frozen, actual=actual_frz)
        if not _near(state.reserved, actual_rsv):
            _issue(issues, "PROJECTION_RESERVED_MISMATCH", "error",
                   f"账户 {account.id} 占用额重放值 {state.reserved:.4f} 与实际 {actual_rsv:.4f} 不一致",
                   account_id=account.id, replayed=state.reserved, actual=actual_rsv)
        for unknown in sorted(state.unknown_types):
            _issue(issues, "PROJECTION_UNKNOWN_EVENT", "warning",
                   f"账户 {account.id} 存在未登记语义的事件类型 {unknown}，未纳入余额重放",
                   account_id=account.id, event_type=unknown)

    # 检查点陈旧（last_seq 落后于事件流）——派生数据，可自动修复
    for cp in db.query(LedgerCheckpoint).all():
        if account_ids_scope := [a.id for a in accounts]:
            if cp.account_id not in account_ids_scope:
                continue
        latest = (
            db.query(func.max(LedgerEvent.seq))
            .filter(LedgerEvent.account_id == cp.account_id)
            .scalar()
        ) or 0
        if int(cp.last_seq) < int(latest):
            _issue(issues, "CHECKPOINT_STALE", "warning",
                   f"账户 {cp.account_id} 检查点停在 seq={cp.last_seq}，事件流已到 {latest}，"
                   "增量重放前需重建检查点",
                   account_id=cp.account_id, checkpoint_seq=cp.last_seq, latest_seq=latest)
    return checked, len(accounts)


# --------------------------------------------------------------------------- #
# 3. 流水勾稽：期初 + 有符号流水 == 当前余额；逐笔快照链
# --------------------------------------------------------------------------- #

def check_flows(db: Session, issues: list[dict], *,
                company_id: int | None, year: int | None) -> None:
    q = db.query(AllowanceTransaction)
    if company_id is not None:
        q = q.filter(AllowanceTransaction.company_id == company_id)
    if year is not None:
        q = q.join(AllowanceAccount, AllowanceTransaction.account_id == AllowanceAccount.id) \
             .filter(AllowanceAccount.year == year)
    for tx in q.order_by(AllowanceTransaction.id.asc()).all():
        sem = semantics_for(tx.tx_type)
        if sem.domain == "unknown" and tx.tx_type not in SEMANTICS:
            _issue(issues, "FLOW_UNKNOWN_TYPE", "warning",
                   f"流水 {tx.id} 类型 {tx.tx_type} 未登记账本语义",
                   tx_id=tx.id, tx_type=tx.tx_type)

    accounts_q = db.query(AllowanceAccount)
    if company_id is not None:
        accounts_q = accounts_q.filter(AllowanceAccount.company_id == company_id)
    if year is not None:
        accounts_q = accounts_q.filter(AllowanceAccount.year == year)

    for account in accounts_q.all():
        rows = (
            db.query(AllowanceTransaction.tx_type,
                     func.coalesce(func.sum(AllowanceTransaction.amount), 0))
            .filter(AllowanceTransaction.account_id == account.id)
            .group_by(AllowanceTransaction.tx_type)
            .all()
        )
        # 勾稽口径：current = opening_balance + Σ 有符号流水（不含 allocation）。
        # 分配在业务层既计入 opening_balance 又写 allocation 流水（期初调整性质），
        # 纳入合计会把分配量计算两次；allocation 流水与 opening 的增量一致性另行核对。
        delta = 0.0
        allocation_sum = 0.0
        for tx_type, total in rows:
            if tx_type == "allocation":
                allocation_sum = round(float(total or 0), 4)
                continue
            delta += amount_signed_for_balance(tx_type, float(total or 0))
        expected_current = round(float(account.opening_balance) + delta, 4)
        if not _near(expected_current, float(account.current_balance)):
            _issue(issues, "FLOW_OPENING_PLUS_TX_MISMATCH", "error",
                   f"账户 {account.id} 期初 {float(account.opening_balance):.4f} + 流水净额 "
                   f"{delta:.4f} = {expected_current:.4f}，与持仓 {float(account.current_balance):.4f} 不符",
                   account_id=account.id, expected=expected_current,
                   actual=float(account.current_balance))

        # 期初值只能因“配额分配/补充分配”而增长：账户全部 allocation 流水合计
        # 应等于 opening_balance（旧库手工建账可能例外，按 warning 披露）。
        if not _near(allocation_sum, float(account.opening_balance), eps=1e-4):
            _issue(issues, "FLOW_OPENING_ALLOCATION_MISMATCH", "warning",
                   f"账户 {account.id} 期初 {float(account.opening_balance):.4f} 与 allocation "
                   f"流水合计 {allocation_sum:.4f} 不一致（可能为旧库手工期初）",
                   account_id=account.id, opening=float(account.opening_balance),
                   allocation=allocation_sum)

        # 逐笔快照链：按 id 排序后每笔快照必须等于前一快照 + 本笔有符号作用
        prev: tuple[float, float | None, float | None] = (
            float(account.opening_balance), None, None,
        )
        for tx in (
            db.query(AllowanceTransaction)
            .filter(AllowanceTransaction.account_id == account.id)
            .order_by(AllowanceTransaction.id.asc())
            .all()
        ):
            from app.core.event_semantics import apply_vector
            vc, vf, vr = apply_vector(tx.tx_type, float(tx.amount))
            bal = float(tx.balance_after)
            frz = float(tx.frozen_after)
            rsv = float(tx.reserved_after)
            if prev[1] is not None and not _near(bal, round(prev[0] + vc, 4)):
                _issue(issues, "FLOW_SNAPSHOT_CHAIN_BROKEN", "warning",
                       f"流水 {tx.id} 的持仓快照 {bal:.4f} 不等于上笔快照 {prev[0]:.4f} 叠加本笔 "
                       f"{vc:+.4f}（可能为旧记录精度/修复导致）",
                       tx_id=tx.id, account_id=account.id)
            if prev[1] is not None and not _near(frz, round(prev[1] + vf, 4)):
                _issue(issues, "FLOW_SNAPSHOT_FROZEN_BROKEN", "warning",
                       f"流水 {tx.id} 的冻结快照链不衔接", tx_id=tx.id, account_id=account.id)
            if prev[2] is not None and not _near(rsv, round(prev[2] + vr, 4)):
                _issue(issues, "FLOW_SNAPSHOT_RESERVED_BROKEN", "warning",
                       f"流水 {tx.id} 的占用快照链不衔接", tx_id=tx.id, account_id=account.id)
            prev = (bal, frz, rsv)


# --------------------------------------------------------------------------- #
# 4. 单据 ↔ 账本
# --------------------------------------------------------------------------- #

def _sum_tx(db: Session, *, account_id: int | None = None, tx_types: tuple[str, ...],
            trade_order_id: int | None = None, auction_trade_id: int | None = None,
            company_id: int | None = None, year: int | None = None) -> float:
    q = db.query(func.coalesce(func.sum(AllowanceTransaction.amount), 0)).filter(
        AllowanceTransaction.tx_type.in_(tx_types)
    )
    if account_id is not None:
        q = q.filter(AllowanceTransaction.account_id == account_id)
    if trade_order_id is not None:
        q = q.filter(AllowanceTransaction.trade_order_id == trade_order_id)
    if auction_trade_id is not None:
        q = q.filter(AllowanceTransaction.auction_trade_id == auction_trade_id)
    if company_id is not None:
        q = q.filter(AllowanceTransaction.company_id == company_id)
    if year is not None:
        q = q.join(AllowanceAccount, AllowanceTransaction.account_id == AllowanceAccount.id) \
             .filter(AllowanceAccount.year == year)
    return round(float(q.scalar() or 0), 4)


def check_documents(db: Session, issues: list[dict], *,
                    company_id: int | None, year: int | None) -> None:
    # 4.1 企业间订单：占用必被释放或随交割出库；交割双方配对且金额等于订单量
    order_q = db.query(TradeOrder)
    if company_id is not None:
        order_q = order_q.filter(
            (TradeOrder.buyer_id == company_id) | (TradeOrder.seller_id == company_id)
        )
    if year is not None:
        order_q = order_q.filter(TradeOrder.year == year)
    for order in order_q.all():
        reserved = _sum_tx(db, tx_types=("trade_reserve",), trade_order_id=order.id)
        released = _sum_tx(db, tx_types=("trade_release",), trade_order_id=order.id)
        out = _sum_tx(db, tx_types=("trade_deliver_out",), trade_order_id=order.id)
        inn = _sum_tx(db, tx_types=("trade_deliver_in",), trade_order_id=order.id)
        amount = float(order.amount)

        if not _near(reserved, released + out):
            _issue(issues, "ORDER_RESERVE_NOT_CLOSED", "error",
                   f"订单 {order.order_no} 占用 {reserved:.4f} ≠ 释放 {released:.4f} + 出库 {out:.4f}",
                   order_id=order.id, reserved=reserved, released=released, delivered_out=out)
        if order.status == "delivered":
            if not _near(out, amount) or not _near(inn, amount):
                _issue(issues, "ORDER_DELIVER_AMOUNT_MISMATCH", "error",
                       f"订单 {order.order_no} 已交割但出库 {out:.4f}/到账 {inn:.4f} "
                       f"与订单量 {amount:.4f} 不一致",
                       order_id=order.id, out=out, inn=inn, amount=amount)
        elif order.status == "confirmed":
            if not _near(reserved - released, amount):
                _issue(issues, "ORDER_CONFIRMED_RESERVE_MISMATCH", "error",
                       f"订单 {order.order_no} 处于 confirmed 但净占用不等于订单量",
                       order_id=order.id)
        elif order.status == "cancelled" and not _near(released, reserved):
            _issue(issues, "ORDER_CANCEL_RESERVE_LEAK", "error",
                   f"订单 {order.order_no} 已撤销但占用未足额释放", order_id=order.id)

    # 4.2 竞价场次/成交单
    trade_q = db.query(AuctionTrade)
    if company_id is not None:
        trade_q = trade_q.filter(
            (AuctionTrade.buyer_id == company_id) | (AuctionTrade.seller_id == company_id)
        )
    if year is not None:
        trade_q = trade_q.filter(AuctionTrade.year == year)
    for trade in trade_q.all():
        qty = float(trade.quantity)
        out = _sum_tx(db, tx_types=("auction_deliver_out",), auction_trade_id=trade.id)
        inn = _sum_tx(db, tx_types=("auction_deliver_in",), auction_trade_id=trade.id)
        if trade.status in ("settled", "reversed", "defaulted"):
            if not _near(out, qty) or not _near(inn, qty):
                _issue(issues, "AUCTION_TRADE_DELIVER_MISMATCH", "error",
                       f"成交单 {trade.trade_no} 状态 {trade.status} 但出库 {out:.4f}/到账 {inn:.4f} "
                       f"与成交量 {qty:.4f} 不符",
                       trade_id=trade.id, out=out, inn=inn, quantity=qty)

        # 冲正累计：明细表合计 == 成交单 reversed_quantity，且 ≤ 成交量
        rev_sum = round(float(
            db.query(func.coalesce(func.sum(AuctionTradeReversal.quantity), 0))
            .filter(AuctionTradeReversal.trade_id == trade.id).scalar() or 0
        ), 4)
        reversed_qty = float(trade.reversed_quantity or 0)
        if not _near(rev_sum, reversed_qty):
            _issue(issues, "AUCTION_REVERSAL_TOTAL_MISMATCH", "error",
                   f"成交单 {trade.trade_no} 冲正明细合计 {rev_sum:.4f} 与累计冲正量 "
                   f"{reversed_qty:.4f} 不一致",
                   trade_id=trade.id, reversal_sum=rev_sum, reversed_quantity=reversed_qty)
        if reversed_qty - qty > _EPS:
            _issue(issues, "AUCTION_REVERSAL_EXCEEDS_QUANTITY", "error",
                   f"成交单 {trade.trade_no} 累计冲正 {reversed_qty:.4f} 超过成交量 {qty:.4f}",
                   trade_id=trade.id)

        # 违约：defaulted_amount == 明细违约合计；repaid == 追偿流水合计
        default_sum = round(float(
            db.query(func.coalesce(func.sum(AuctionTradeReversal.defaulted_quantity), 0))
            .filter(AuctionTradeReversal.trade_id == trade.id).scalar() or 0
        ), 4)
        if not _near(default_sum, float(trade.defaulted_amount or 0)):
            _issue(issues, "AUCTION_DEFAULT_TOTAL_MISMATCH", "error",
                   f"成交单 {trade.trade_no} 违约明细 {default_sum:.4f} 与欠额登记 "
                   f"{float(trade.defaulted_amount or 0):.4f} 不一致", trade_id=trade.id)
        repaid_sum = _sum_tx(db, tx_types=("auction_default_repay_out",), auction_trade_id=trade.id)
        repaid_docs = round(float(
            db.query(func.coalesce(func.sum(AuctionDefaultRepayment.quantity), 0))
            .filter(AuctionDefaultRepayment.trade_id == trade.id).scalar() or 0
        ), 4)
        if not _near(repaid_sum, repaid_docs) or not _near(repaid_sum, float(trade.repaid_amount or 0)):
            _issue(issues, "AUCTION_REPAY_TOTAL_MISMATCH", "error",
                   f"成交单 {trade.trade_no} 追偿流水 {repaid_sum:.4f}/追偿单 {repaid_docs:.4f}/"
                   f"成交单已偿 {float(trade.repaid_amount or 0):.4f} 三处不一致", trade_id=trade.id)

        # 成交单归属补缴净额 ≥ 0（同一吨补缴最多退还一次，两条回退链路共用一本账）
        cleared = _sum_tx(db, tx_types=("auction_deficit_clear",), auction_trade_id=trade.id)
        refunded = _sum_tx(db, tx_types=("auction_clear_refund",), auction_trade_id=trade.id)
        if refunded - cleared > _EPS:
            _issue(issues, "AUCTION_CLEAR_REFUND_EXCEEDED", "error",
                   f"成交单 {trade.trade_no} 归属补缴退还 {refunded:.4f} 超过补缴 {cleared:.4f}，"
                   "同一吨被重复退还，系统总配额不再守恒",
                   trade_id=trade.id, cleared=cleared, refunded=refunded)

        # 冲正量恒等式：冲正量 = 自买方收回（已划付卖方）+ 违约欠额登记；
        # 欠额随后由追偿流水偿还（repaid），与欠额登记单独勾稽。
        # 即部分冲正时“拿得回的 + 拿不回立欠据的”必须等于申请回退量。
        clawback = _sum_tx(db, tx_types=("auction_clawback_out",), auction_trade_id=trade.id)
        defaulted_total = round(float(trade.defaulted_amount or 0), 4)
        if not _near(clawback + defaulted_total, reversed_qty):
            _issue(issues, "AUCTION_REVERSAL_RECOVERY_IDENTITY", "error",
                   f"成交单 {trade.trade_no} 收回 {clawback:.4f} + 违约欠额登记 "
                   f"{defaulted_total:.4f} ≠ 冲正量 {reversed_qty:.4f}", trade_id=trade.id)
        if float(trade.repaid_amount or 0) - defaulted_total > _EPS:
            _issue(issues, "AUCTION_REPAY_EXCEEDS_DEFAULT", "error",
                   f"成交单 {trade.trade_no} 追偿 {float(trade.repaid_amount or 0):.4f} "
                   f"超过违约欠额 {defaulted_total:.4f}", trade_id=trade.id)

    # 4.3 场次：成交量/状态
    session_q = db.query(AuctionSession)
    if year is not None:
        session_q = session_q.filter(AuctionSession.year == year)
    for session in session_q.all():
        matched = round(float(
            db.query(func.coalesce(func.sum(AuctionTrade.quantity), 0))
            .filter(AuctionTrade.session_id == session.id).scalar() or 0
        ), 4)
        if not _near(matched, float(session.matched_volume or 0)):
            _issue(issues, "AUCTION_VOLUME_MISMATCH", "warning",
                   f"场次 {session.session_no} 成交单合计 {matched:.4f} 与登记成交量 "
                   f"{float(session.matched_volume or 0):.4f} 不一致",
                   session_id=session.id)
        trade_statuses = {
            r[0] for r in db.query(AuctionTrade.status)
            .filter(AuctionTrade.session_id == session.id).all()
        }
        if session.status == "settled" and trade_statuses and not trade_statuses <= {
            "settled", "reversed", "defaulted", "cancelled"
        }:
            _issue(issues, "AUCTION_SETTLED_WITH_UNSETTLED_TRADES", "error",
                   f"场次 {session.session_no} 已结算但仍存在未成交状态的成交单 {trade_statuses}",
                   session_id=session.id)

    # 4.4 竞价占用总量守恒（企业年度内）：报价占用 = 撤单/余量释放 + 结算出库
    reserve = _sum_tx(db, tx_types=("auction_bid_reserve",), company_id=company_id, year=year)
    release = _sum_tx(
        db, tx_types=("auction_bid_release", "auction_reserve_release"),
        company_id=company_id, year=year,
    )
    deliver_out = _sum_tx(db, tx_types=("auction_deliver_out",), company_id=company_id, year=year)
    if reserve > _EPS and not _near(reserve, release + deliver_out):
        _issue(issues, "AUCTION_RESERVE_NOT_CLOSED", "warning",
               f"竞价报价占用 {reserve:.4f} ≠ 释放 {release:.4f} + 结算出库 {deliver_out:.4f}"
               + ("（指定范围内可能有在途占用）" if company_id is None or year is None else ""),
               company_id=company_id, year=year)


# --------------------------------------------------------------------------- #
# 5. 履约一致
# --------------------------------------------------------------------------- #

def check_compliance(db: Session, issues: list[dict], *,
                     company_id: int | None, year: int | None) -> None:
    record_q = db.query(ComplianceRecord)
    if company_id is not None:
        record_q = record_q.filter(ComplianceRecord.company_id == company_id)
    if year is not None:
        record_q = record_q.filter(ComplianceRecord.year == year)

    for record in record_q.all():
        account = (
            db.query(AllowanceAccount)
            .filter(AllowanceAccount.company_id == record.company_id,
                    AllowanceAccount.year == record.year)
            .first()
        )
        # 归档记录（报告冲正）：其冻结应已全部解冻、清缴应已全部退还
        if not record.is_active:
            if not _near(float(record.frozen_amount or 0), 0) or not _near(float(record.deficit or 0), 0):
                _issue(issues, "COMPLIANCE_ARCHIVED_DIRTY", "error",
                       f"履约记录 {record.id} 已归档但 frozen/deficit 未归零",
                       record_id=record.id)
            continue

        cleared_out = _sum_tx(db, account_id=account.id if account else None,
                              tx_types=_CLEAR_OUT_TYPES)
        refund_in = _sum_tx(db, account_id=account.id if account else None,
                            tx_types=_CLEAR_REFUND_TYPES)
        net_cleared = round(cleared_out - refund_in, 4)
        if not _near(net_cleared, float(record.cleared_amount or 0), eps=1e-4):
            _issue(issues, "COMPLIANCE_CLEARED_MISMATCH", "error",
                   f"企业 {record.company_id}/{record.year} 履约记录清缴 "
                   f"{float(record.cleared_amount or 0):.4f} 与流水净清缴 {net_cleared:.4f} 不一致",
                   record_id=record.id, record_cleared=float(record.cleared_amount or 0),
                   flow_net_cleared=net_cleared)

        if account is not None and not _near(float(account.frozen_balance),
                                             float(record.frozen_amount or 0), eps=1e-4):
            _issue(issues, "COMPLIANCE_FROZEN_MISMATCH", "error",
                   f"企业 {record.company_id}/{record.year} 账户冻结 "
                   f"{float(account.frozen_balance):.4f} 与履约记录冻结 "
                   f"{float(record.frozen_amount or 0):.4f} 不一致",
                   record_id=record.id, account_id=account.id)

        # 缺口/状态自洽
        emission = float(record.verified_emission or 0)
        cleared = float(record.cleared_amount or 0)
        frozen = float(record.frozen_amount or 0)
        expected_deficit = round(emission - cleared - frozen, 4)
        if not _near(expected_deficit, float(record.deficit or 0), eps=1e-4):
            _issue(issues, "COMPLIANCE_DEFICIT_ARITHMETIC", "error",
                   f"履约记录 {record.id} 缺口 {float(record.deficit or 0):.4f} ≠ 排放 "
                   f"{emission:.4f} − 清缴 {cleared:.4f} − 冻结 {frozen:.4f} = {expected_deficit:.4f}",
                   record_id=record.id)
        if record.status == "compliant" and float(record.deficit or 0) > 1e-4:
            _issue(issues, "COMPLIANCE_STATUS_DEFICIT_CONFLICT", "error",
                   f"履约记录 {record.id} 标记 compliant 但仍有缺口", record_id=record.id)

        # 配额状态可由履约状态推导
        quota = (
            db.query(Quota)
            .filter(Quota.company_id == record.company_id, Quota.year == record.year)
            .first()
        )
        if quota is not None:
            if float(record.deficit or 0) <= 1e-4:
                want = "cleared" if cleared > 0 else "frozen"
            else:
                want = "allocated"
            if quota.status not in (want, "cleared"):
                _issue(issues, "QUOTA_STATUS_INCONSISTENT", "warning",
                       f"企业 {record.company_id}/{record.year} 配额状态 {quota.status} "
                       f"与履约推导状态 {want} 不一致（记录状态 {record.status}）",
                       quota_status=quota.status, expected=want, record_id=record.id)

    # 报告 ↔ 履约记录：批准必有活跃记录；冲正必归档
    report_q = db.query(MrvReport)
    if company_id is not None:
        report_q = report_q.filter(MrvReport.company_id == company_id)
    if year is not None:
        report_q = report_q.filter(MrvReport.year == year)
    for report in report_q.all():
        linked = (
            db.query(ComplianceRecord)
            .filter(ComplianceRecord.report_id == report.id)
            .all()
        )
        active = [r for r in linked if r.is_active]
        if report.status == "approved" and not active:
            _issue(issues, "REPORT_APPROVED_WITHOUT_ACTIVE_RECORD", "error",
                   f"报告 {report.id} 已批准但无活跃履约记录", report_id=report.id)
        if report.status == "reversed" and active:
            _issue(issues, "REPORT_REVERSED_WITH_ACTIVE_RECORD", "error",
                   f"报告 {report.id} 已冲正但仍存在活跃履约记录", report_id=report.id)


# --------------------------------------------------------------------------- #
# 6. 系统守恒与跨年度
# --------------------------------------------------------------------------- #

def check_conservation(db: Session, issues: list[dict], *,
                       company_id: int | None, year: int | None) -> dict[str, float]:
    summary: dict[str, float] = {}
    # 6.1 内部配对：按全局/年度汇总，出入必须相等
    for name, (out_type, in_type) in _PAIRS.items():
        out_total = _sum_tx(db, tx_types=(out_type,), company_id=company_id, year=year)
        in_total = _sum_tx(db, tx_types=(in_type,), company_id=company_id, year=year)
        summary[f"{name}_out"] = out_total
        summary[f"{name}_in"] = in_total
        if not _near(out_total, in_total):
            _issue(issues, "CONSERVATION_PAIR_UNBALANCED", "error",
                   f"{name} 划出合计 {out_total:.4f} ≠ 到账合计 {in_total:.4f}，"
                   "跨主体划转有配额凭空消失/增加",
                   pair=name, out=out_total, inn=in_total, year=year)

    # 6.2 逐年度系统总配额恒等式（外部市场 buy/sell/transfer 允许改变系统总量）：
    # 期末总持仓 + 净清缴离仓 = 期初 + 外部净流入
    account_q = (
        db.query(
            AllowanceAccount.year,
            func.coalesce(func.sum(AllowanceAccount.opening_balance), 0),
            func.coalesce(func.sum(AllowanceAccount.current_balance), 0),
        )
        .group_by(AllowanceAccount.year)
    )
    if year is not None:
        account_q = account_q.filter(AllowanceAccount.year == year)
    if company_id is not None:
        account_q = account_q.filter(AllowanceAccount.company_id == company_id)
    for acc_year, opening_total, current_total in account_q.all():
        allocated = _sum_tx(db, tx_types=("allocation",), year=acc_year,
                            company_id=company_id)
        # 年度总配额恒等式（直接取自语义注册表，任何流水类型都不会漏算）：
        # 期末总持仓 = 期初 + Σ 全部有符号流水（allocation 除外，已含在期初）。
        # 其中内部跨主体划转在全平台范围两两抵消（6.1 已独立校验配对相等），
        # 净清缴离仓、外部市场买卖、冲正退还等全部体现在流水净额里。
        acct_filter_q = db.query(AllowanceAccount.id).filter(AllowanceAccount.year == acc_year)
        if company_id is not None:
            acct_filter_q = acct_filter_q.filter(AllowanceAccount.company_id == company_id)
        account_ids = [row[0] for row in acct_filter_q.all()]
        net_flow = 0.0
        net_cleared = 0.0
        if account_ids:
            type_rows = (
                db.query(
                    AllowanceTransaction.tx_type,
                    func.coalesce(func.sum(AllowanceTransaction.amount), 0),
                )
                .filter(AllowanceTransaction.account_id.in_(account_ids))
                .group_by(AllowanceTransaction.tx_type)
                .all()
            )
            for tx_type, total in type_rows:
                signed = amount_signed_for_balance(tx_type, float(total or 0))
                if tx_type != "allocation":
                    net_flow = round(net_flow + signed, 4)
                if tx_type in _CLEAR_OUT_TYPES:
                    net_cleared = round(net_cleared + float(total or 0), 4)
                elif tx_type in _CLEAR_REFUND_TYPES:
                    net_cleared = round(net_cleared - float(total or 0), 4)
        rhs = round(float(opening_total) + net_flow, 4)
        summary[f"year_{acc_year}_opening"] = float(opening_total)
        summary[f"year_{acc_year}_current"] = float(current_total)
        summary[f"year_{acc_year}_net_flow"] = net_flow
        summary[f"year_{acc_year}_net_cleared"] = net_cleared
        summary[f"year_{acc_year}_allocation"] = allocated
        if not _near(rhs, float(current_total), eps=1e-4):
            _issue(issues, "CONSERVATION_SYSTEM_TOTAL", "error",
                   f"{acc_year} 年度总配额不守恒：期初 {float(opening_total):.4f} "
                   f"+ 流水净额 {net_flow:.4f} = {rhs:.4f}，实际总持仓 "
                   f"{float(current_total):.4f}",
                   year=acc_year, expected=rhs, actual=float(current_total))

    # 6.3 跨年度：所有余额事件必须带年度；单据年度与账户年度一致
    missing_year = (
        db.query(func.count(LedgerEvent.id))
        .filter(LedgerEvent.direction != "status", LedgerEvent.year.is_(None))
        .scalar()
    )
    if missing_year:
        _issue(issues, "CROSS_YEAR_EVENT_WITHOUT_YEAR", "error",
               f"{missing_year} 条余额事件缺少年度归属，跨年度状态无法隔离核对",
               count=int(missing_year))

    for tx in db.query(AllowanceTransaction).filter(
        AllowanceTransaction.trade_order_id.isnot(None)
    ).all():
        order = db.get(TradeOrder, tx.trade_order_id)
        account = db.get(AllowanceAccount, tx.account_id)
        if order and account and order.year != account.year:
            _issue(issues, "CROSS_YEAR_ORDER_ACCOUNT_MISMATCH", "error",
                   f"流水 {tx.id} 所在账户年度 {account.year} 与订单 {order.order_no} "
                   f"年度 {order.year} 不一致，跨年度串账",
                   tx_id=tx.id, order_id=order.id)
    return summary


# --------------------------------------------------------------------------- #
# 主入口
# --------------------------------------------------------------------------- #

def run_reconciliation(
    db: Session,
    *,
    scope: str = "full",
    company_id: int | None = None,
    year: int | None = None,
    idempotency_key: str | None = None,
    triggered_by: int | None = None,
    rebuild_stale_checkpoints: bool = False,
) -> LedgerReconciliation:
    """执行一次对账并持久化运行记录。幂等键重复触发返回首次结果。

    对账只读业务数据（不修改任何余额/流水/单据）；唯一写操作是运行记录本身，
    以及可选的检查点重建（派生投影，可随时丢弃重建）。
    """
    if scope not in ("full", "company", "year"):
        raise ValueError("scope 仅支持 full/company/year")

    with _recon_guard:
        if idempotency_key:
            existing = (
                db.query(LedgerReconciliation)
                .filter(LedgerReconciliation.idempotency_key == idempotency_key)
                .first()
            )
            if existing:
                return existing

        run = LedgerReconciliation(
            recon_no=_gen_recon_no(db),
            scope=scope,
            company_id=company_id,
            year=year,
            status="running",
            idempotency_key=idempotency_key,
            triggered_by=triggered_by,
        )
        db.add(run)
        db.flush()

        try:
            issues: list[dict] = []
            checked_events = check_chain(db, issues)
            states = replay_all(db, company_id=company_id, year=year, check_snapshots=True)
            # 全量重放的逐笔快照不符也是差异
            for account_id, state in states.items():
                for item in state.snapshot_mismatches:
                    tx_id, event_type, is_legacy, replayed, snapshot = item
                    _issue(
                        issues,
                        "REPLAY_SNAPSHOT_MISMATCH" if not is_legacy else "REPLAY_LEGACY_SNAPSHOT_MISMATCH",
                        "error" if not is_legacy else "warning",
                        f"账户 {account_id} 事件 {tx_id}（{event_type}）重放余额 {replayed} "
                        f"与落账快照 {snapshot} 不一致"
                        + ("（旧记录，按 warning 披露）" if is_legacy else ""),
                        account_id=account_id, event_id=tx_id,
                        replayed=list(replayed), snapshot=list(snapshot),
                    )

            checked_accounts, _ = check_projections(
                db, issues, states, company_id=company_id, year=year
            )
            check_flows(db, issues, company_id=company_id, year=year)
            check_documents(db, issues, company_id=company_id, year=year)
            check_compliance(db, issues, company_id=company_id, year=year)
            summary = check_conservation(db, issues, company_id=company_id, year=year)

            if rebuild_stale_checkpoints and any(
                i["code"] == "CHECKPOINT_STALE" for i in issues
            ):
                from app.services.replay_service import rebuild_checkpoints

                rebuild_checkpoints(db, commit=False)
                issues = [i for i in issues if i["code"] != "CHECKPOINT_STALE"]
                summary["checkpoints_rebuilt"] = 1

            errors = [i for i in issues if i["severity"] == "error"]
            run.status = "balanced" if not errors else "discrepancy"
            run.checked_accounts = checked_accounts
            run.checked_events = checked_events
            run.discrepancy_count = len(issues)
            run.conserved = 0 if any(
                i["code"].startswith("CONSERVATION_") for i in errors
            ) else 1
            run.discrepancies_json = json.dumps(issues, ensure_ascii=False)
            run.summary_json = json.dumps(summary, ensure_ascii=False)
            run.finished_at = datetime.utcnow()
            db.flush()
            db.commit()
            db.refresh(run)
        except IntegrityError:
            db.rollback()
            if idempotency_key:
                existing = (
                    db.query(LedgerReconciliation)
                    .filter(LedgerReconciliation.idempotency_key == idempotency_key)
                    .first()
                )
                if existing:
                    return existing
            raise
        except Exception:
            db.rollback()
            run = (
                db.query(LedgerReconciliation)
                .filter(LedgerReconciliation.id == run.id)
                .first()
            )
            if run is not None:
                run.status = "failed"
                run.finished_at = datetime.utcnow()
                db.commit()
            raise
        return run


def serialize_run(run: LedgerReconciliation) -> dict:
    return {
        "id": run.id,
        "recon_no": run.recon_no,
        "scope": run.scope,
        "company_id": run.company_id,
        "year": run.year,
        "status": run.status,
        "checked_accounts": run.checked_accounts,
        "checked_events": run.checked_events,
        "discrepancy_count": run.discrepancy_count,
        "conserved": bool(run.conserved),
        "discrepancies": json.loads(run.discrepancies_json or "[]"),
        "summary": json.loads(run.summary_json or "{}"),
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
    }
