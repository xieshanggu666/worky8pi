"""统一账本事件语义注册表：每种流水/业务事件对（持仓、冻结、占用）的作用方向。

这是整个“重放与对账链路”的单一事实来源：

- 业务层（quota_service / trade_order_service / auction_service）定义了
  20+ 种 ``tx_type``，各自调用 ``apply_ledger_delta`` 的方式分散在多处；
- 这里把每种类型的 **有符号投影规则** 集中登记一次，重放器、事件登记器、
  对账器全部引用同一张表，杜绝“业务记账一套语义、重放又是另一套语义”的漂移。

约定（amount 始终为正数，符号由 direction 决定）：

- credit(+c)：持仓增加；debit(-c)：持仓减少；
- freeze/unfreeze：冻结额增减，持仓不变；
- reserve/release：占用额增减，持仓不变；
- deliver_out / deliver_release 类组合动作用元组表达，如
  ``auction_deliver_out = (debit, 0, release)``：持仓与占用同减；
- status：业务状态流转事件（不作用于余额投影）。

组合规则以三元组 ``(dc, fr, rs)`` 表示对 current/frozen/reserved 的系数：
+amount / -amount / 0。
"""

from __future__ import annotations

from dataclasses import dataclass


# 方向 -> (current 系数, frozen 系数, reserved 系数)，作用时乘以 amount
_DIRECTION_VECTORS: dict[str, tuple[int, int, int]] = {
    "credit": (1, 0, 0),
    "debit": (-1, 0, 0),
    "freeze": (0, 1, 0),
    "unfreeze": (0, -1, 0),
    "reserve": (0, 0, 1),
    "release": (0, 0, -1),
    # 组合方向：持仓与占用同减（占用出库）
    "debit_release": (-1, 0, -1),
    # 组合方向：持仓与冻结同减（冻结核销出库：冻结配额履约离仓）
    "debit_unfreeze": (-1, -1, 0),
    "status": (0, 0, 0),
}


@dataclass(frozen=True)
class EventSemantics:
    """一种事件类型的账本语义。"""

    event_type: str
    direction: str
    # 业务域：quota/order/auction/compliance/system（未知旧类型为 unknown）
    domain: str
    # 是否跨主体配对事件（出账方与入账方各一条，金额应相等）
    paired: bool = False
    # 配对视角：out=划出方，in=划入方；单端事件为空
    pair_side: str = ""
    # 是否为冲正/补偿类事件（不删旧事件，追加反向作用）
    compensating: bool = False
    description: str = ""

    @property
    def vector(self) -> tuple[int, int, int]:
        return _DIRECTION_VECTORS[self.direction]


def v(event_type: str, direction: str, domain: str, **kw) -> EventSemantics:
    return EventSemantics(event_type=event_type, direction=direction, domain=domain, **kw)


# --------------------------------------------------------------------------- #
# 流水事件（与 allowance_transactions.tx_type 对齐）
# --------------------------------------------------------------------------- #
SEMANTICS: dict[str, EventSemantics] = {
    # 配额分配：主管部门 -> 企业（系统外注入，企业端入账）
    "allocation": v("allocation", "credit", "quota", description="免费配额分配入账"),
    # 台账划转（兼容旧版买入/卖出/划转，对手方为自由文本，系统级配对校验放宽）
    "buy": v("buy", "credit", "quota", description="台账买入"),
    "sell": v("sell", "debit", "quota", description="台账卖出"),
    "transfer_in": v("transfer_in", "credit", "quota", paired=True, pair_side="in",
                     description="划转受让到账"),
    "transfer_out": v("transfer_out", "debit", "quota", paired=True, pair_side="out",
                      description="划转划出"),
    # 履约冻结 / 解冻
    "freeze": v("freeze", "freeze", "compliance", description="报告批准冻结履约配额"),
    "reversal_unfreeze": v("reversal_unfreeze", "unfreeze", "compliance", compensating=True,
                           description="报告冲正解除冻结"),
    "auction_clear_unfreeze": v("auction_clear_unfreeze", "unfreeze", "compliance",
                                compensating=True, description="竞价冲正解除冻结核销"),
    # 清缴出库（持仓减少）；frozen_clear 同时核销冻结（持仓与冻结同减）
    "clear": v("clear", "debit", "compliance", description="可用配额履约清缴"),
    "frozen_clear": v("frozen_clear", "debit_unfreeze", "compliance",
                      description="冻结配额履约清缴（持仓与冻结同减）"),
    "trade_deficit_clear": v("trade_deficit_clear", "debit", "compliance",
                             description="订单交割到账配额补缴缺口"),
    "auction_deficit_clear": v("auction_deficit_clear", "debit", "compliance",
                               description="竞价到账配额补缴缺口"),
    # 冲正退还/补偿
    "reversal": v("reversal", "credit", "compliance", compensating=True,
                  description="报告冲正退还已清缴配额"),
    "auction_clear_refund": v("auction_clear_refund", "credit", "compliance", compensating=True,
                              description="回退竞价到账补缴（成交单归属）"),
    # 企业间订单
    "trade_reserve": v("trade_reserve", "reserve", "order", description="订单确认占用"),
    "trade_release": v("trade_release", "release", "order", compensating=True,
                       description="订单撤销释放占用"),
    "trade_deliver_out": v("trade_deliver_out", "debit_release", "order", paired=True,
                           pair_side="out", description="订单交割划出（占用出库）"),
    "trade_deliver_in": v("trade_deliver_in", "credit", "order", paired=True, pair_side="in",
                          description="订单交割到账"),
    # 集中竞价
    "auction_bid_reserve": v("auction_bid_reserve", "reserve", "auction",
                             description="卖出报价占用"),
    "auction_bid_release": v("auction_bid_release", "release", "auction", compensating=True,
                             description="撤单/未成交释放报价占用"),
    "auction_reserve_release": v("auction_reserve_release", "release", "auction",
                                 compensating=True, description="撮合余量/撤场释放占用"),
    "auction_deliver_out": v("auction_deliver_out", "debit_release", "auction", paired=True,
                             pair_side="out", description="竞价结算划出（占用出库）"),
    "auction_deliver_in": v("auction_deliver_in", "credit", "auction", paired=True,
                            pair_side="in", description="竞价结算到账"),
    "auction_clawback_out": v("auction_clawback_out", "debit", "auction", paired=True,
                              pair_side="out", compensating=True, description="冲正自买方收回"),
    "auction_clawback_in": v("auction_clawback_in", "credit", "auction", paired=True,
                             pair_side="in", compensating=True, description="冲正划付卖方"),
    "auction_default_repay_out": v("auction_default_repay_out", "debit", "auction",
                                   paired=True, pair_side="out", description="违约追偿划出"),
    "auction_default_repay_in": v("auction_default_repay_in", "credit", "auction",
                                  paired=True, pair_side="in", description="违约追偿到账"),
    # 历史遗留类型（旧记录兼容）
    "offset": v("offset", "debit", "unknown", description="历史抵销清缴（旧类型）"),
}


# 纯业务状态流转事件（不对应流水，只记录业务单据生命周期）
BUSINESS_STATUS_EVENTS: dict[str, str] = {
    # event_type -> domain
    "order_status": "order",
    "auction_session_status": "auction",
    "auction_bid_status": "auction",
    "auction_trade_status": "auction",
    "compliance_status": "compliance",
    "report_status": "compliance",
}


def semantics_for(event_type: str) -> EventSemantics:
    """返回事件语义；未登记类型降级为 unknown/status（不阻断重放，转人工核对）。"""
    known = SEMANTICS.get(event_type)
    if known is not None:
        return known
    if event_type in BUSINESS_STATUS_EVENTS:
        return EventSemantics(
            event_type=event_type,
            direction="status",
            domain=BUSINESS_STATUS_EVENTS[event_type],
            description="业务单据状态流转",
        )
    return EventSemantics(
        event_type=event_type,
        direction="status",
        domain="unknown",
        description="未登记的历史事件类型，跳过余额投影仅作记录",
    )


def apply_vector(event_type: str, amount: float) -> tuple[float, float, float]:
    """给定事件类型与金额，返回对 ``(current, frozen, reserved)`` 的有符号增量。"""
    dc, fr, rs = semantics_for(event_type).vector
    a = float(amount)
    return (dc * a, fr * a, rs * a)


def amount_signed_for_balance(event_type: str, amount: float) -> float:
    """事件对持仓 current 的有符号影响（对账汇总用）。"""
    return apply_vector(event_type, amount)[0]
