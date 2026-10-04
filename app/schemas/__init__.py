from datetime import datetime

from pydantic import BaseModel, Field


class LoginIn(BaseModel):
    username: str
    password: str


class CompanyIn(BaseModel):
    code: str
    name: str
    industry: str = ""
    region: str = ""
    boundary_desc: str = ""


class ScopeIn(BaseModel):
    scope: str = Field(pattern="^[123]$")
    category: str = ""
    name: str = ""
    description: str = ""


class ActivityIn(BaseModel):
    scope_id: int
    year: int
    period: str = "monthly"
    activity_type: str
    unit: str = ""
    quantity: float
    data_source: str = ""


class ActivityBatchVerifyIn(BaseModel):
    """批量核验活动数据：按 id 列表或 企业+年度 圈定（id 优先）。

    - ``recalculate``：核验后在同一事务内对受影响的企业年度重算排放量并联动草稿报告；
    - ``idempotency_key``：可选幂等键（核验本身为状态收敛操作，重发结果一致）。
    """

    activity_ids: list[int] | None = None
    company_id: int | None = None
    year: int | None = None
    recalculate: bool = True
    idempotency_key: str | None = None


class FactorIn(BaseModel):
    factor_code: str
    name: str
    scope: str = Field(default="1", pattern="^[123]$")
    unit: str = "tCO2/单位"
    value: float
    source: str = ""
    valid_from: str = ""
    valid_to: str | None = None


class QuotaIn(BaseModel):
    company_id: int
    year: int
    baseline: float = 0
    allocation_amount: float
    adjustment: float = 0


class TransferIn(BaseModel):
    amount: float
    tx_type: str = "sell"
    counterparty: str = ""
    price: float | None = None
    tx_date: str = ""
    remark: str = ""
    # 客户端幂等键：同账户相同键的重复提交只入账一次（也可用 Idempotency-Key 请求头）
    idempotency_key: str | None = None


class TradeOrderIn(BaseModel):
    seller_id: int
    buyer_id: int
    year: int
    amount: float = Field(gt=0)
    price: float = Field(default=0, ge=0)
    # 发起方：seller=卖方挂单 / buyer=买方求购，发起方建单即视为已确认
    initiator: str = Field(default="seller", pattern="^(seller|buyer)$")
    tx_date: str = ""
    remark: str = ""
    idempotency_key: str | None = None
    # 交割时是否自动用买方到账配额核销其同年度履约缺口（默认开启，年度配额闭环）
    auto_clear_deficit: bool = True


class TradeOrderCancelIn(BaseModel):
    reason: str = Field(default="", max_length=256)


class ReportReversalIn(BaseModel):
    reason: str = Field(min_length=2, max_length=500)


class AuctionSessionIn(BaseModel):
    year: int
    name: str = Field(default="", max_length=128)
    reserve_price: float = Field(default=0, ge=0)
    estimated_volume: float | None = Field(default=None, ge=0)
    product: str = Field(default="allowance", pattern="^(allowance|CCER)$")
    auto_clear_deficit: bool = True
    # 后续场次结算到账时，是否自动用买方自由可用追偿其历史违约欠额（默认开启）
    auto_recover_default: bool = True
    remark: str = Field(default="", max_length=256)
    # 传入即“创建并直接开放”；缺省为草稿，监管随后调用开放接口
    open_at: datetime | None = None
    close_at: datetime | None = None
    idempotency_key: str | None = None


class AuctionSessionCancelIn(BaseModel):
    reason: str = Field(default="", max_length=256)


class AuctionBidIn(BaseModel):
    quantity: float = Field(gt=0)
    price: float = Field(ge=0)
    side: str = Field(default="buy", pattern="^(buy|sell)$")
    tx_date: str = ""
    remark: str = Field(default="", max_length=256)
    idempotency_key: str | None = None


class AuctionBidCancelIn(BaseModel):
    reason: str = Field(default="", max_length=256)


class AuctionTradeReversalIn(BaseModel):
    """监管冲正已结算成交单：可整笔/批量/部分数量。"""

    reason: str = Field(min_length=2, max_length=500)
    # 缺省冲正场次全部已结算成交单；指定时仅冲正列表内成交单
    trade_ids: list[int] | None = None
    # 部分回退：{成交单id: 数量}；缺省整笔回退
    quantities: dict[int, float] | None = None
    idempotency_key: str | None = None


class AuctionDefaultRepayIn(BaseModel):
    """监管手动追偿单笔违约欠额；amount 缺省为全额。"""

    amount: float | None = Field(default=None, gt=0)
    idempotency_key: str | None = None
