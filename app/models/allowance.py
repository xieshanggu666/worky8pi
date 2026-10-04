from datetime import datetime

from sqlalchemy import (
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    text,
)

from app.core.database import Base


class Quota(Base):
    """年度配额分配：免费配额与调整。"""

    __tablename__ = "quotas"
    __table_args__ = (
        # 同一企业同一年度只能有一条配额记录，并发分配由数据库兜底幂等
        UniqueConstraint("company_id", "year", name="uq_quota_company_year"),
    )

    id = Column(Integer, primary_key=True)
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=False, index=True)
    year = Column(Integer, nullable=False, index=True)
    baseline = Column(Numeric(18, 4), nullable=False, default=0)      # 历史基准排放
    allocation_amount = Column(Numeric(18, 4), nullable=False, default=0)  # 免费配额（tCO2）
    adjustment = Column(Numeric(18, 4), nullable=False, default=0)    # 调整量（可为负）
    total = Column(Numeric(18, 4), nullable=False, default=0)         # 最终配额
    status = Column(String(16), nullable=False, default="pending")    # pending/allocated/frozen/cleared
    allocated_at = Column(DateTime, nullable=True)


class AllowanceAccount(Base):
    """配额账户：企业年度配额持仓。"""

    __tablename__ = "allowance_accounts"

    id = Column(Integer, primary_key=True)
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=False, index=True)
    year = Column(Integer, nullable=False, index=True)
    opening_balance = Column(Numeric(18, 4), nullable=False, default=0)
    current_balance = Column(Numeric(18, 4), nullable=False, default=0)
    frozen_balance = Column(Numeric(18, 4), nullable=False, default=0)   # 履约冻结
    reserved_balance = Column(Numeric(18, 4), nullable=False, default=0)  # 交易占用（已确认待交割订单）
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)


class AllowanceTransaction(Base):
    """配额划转与交易台账。"""

    __tablename__ = "allowance_transactions"
    __table_args__ = (
        # 客户端幂等键：同一账户重复提交（双击/重试/超时重发）只入账一次。
        # NULL 不参与唯一约束，未携带幂等键的请求不受影响。
        UniqueConstraint("account_id", "idempotency_key", name="uq_tx_account_idem"),
    )

    id = Column(Integer, primary_key=True)
    account_id = Column(Integer, ForeignKey("allowance_accounts.id"), nullable=False, index=True)
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=False, index=True)
    # allocation/buy/sell/transfer_in/transfer_out/freeze/clear/frozen_clear/offset/
    # reversal（报告冲正退还已清缴）/reversal_unfreeze（报告冲正解除冻结）/
    # trade_reserve/trade_release/trade_deliver_out/trade_deliver_in/
    # auction_bid_reserve/auction_bid_release/auction_reserve_release/
    # auction_deliver_out/auction_deliver_in/auction_deficit_clear/
    # auction_reverse_out/auction_reverse_in（结算划转回退）、
    # auction_clear_unfreeze/auction_clear_refund（联动清缴回滚：解冻/退还补缴）、
    # auction_clawback_out/auction_clawback_in（违约买方配额收回并划付卖方）、
    # auction_default_repay_out/auction_default_repay_in（违约补缴追偿）
    tx_type = Column(String(26), nullable=False)
    amount = Column(Numeric(18, 4), nullable=False, default=0)
    counterparty = Column(String(128), nullable=False, default="")
    price = Column(Numeric(18, 2), nullable=True)
    tx_date = Column(String(10), nullable=False, default="")
    balance_after = Column(Numeric(18, 4), nullable=False, default=0)
    frozen_after = Column(Numeric(18, 4), nullable=False, default=0)
    reserved_after = Column(Numeric(18, 4), nullable=False, default=0)  # 交易占用快照
    trade_order_id = Column(Integer, ForeignKey("trade_orders.id"), nullable=True, index=True)  # 关联企业间订单
    auction_trade_id = Column(Integer, ForeignKey("auction_trades.id"), nullable=True, index=True)  # 关联竞价成交单
    remark = Column(String(256), nullable=False, default="")
    idempotency_key = Column(String(64), nullable=True)  # 客户端去重键（UUID），同账户唯一
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class ComplianceRecord(Base):
    """年度履约记录：清缴配额抵扣实际排放。"""

    __tablename__ = "compliance_records"
    __table_args__ = (
        # 仅活跃记录保持企业+年度唯一；报告冲正归档后允许重新批准生成新记录。
        # SQLite/PostgreSQL 使用部分索引，其他数据库降级为普通索引并由应用锁兜底。
        Index(
            "uq_compliance_active_company_year",
            "company_id",
            "year",
            unique=True,
            sqlite_where=text("is_active = 1"),
            postgresql_where=text("is_active = 1"),
        ),
    )

    id = Column(Integer, primary_key=True)
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=False, index=True)
    year = Column(Integer, nullable=False, index=True)
    verified_emission = Column(Numeric(18, 4), nullable=False, default=0)  # 批准报告确认的排放量
    cleared_amount = Column(Numeric(18, 4), nullable=False, default=0)     # 已清缴配额
    frozen_amount = Column(Numeric(18, 4), nullable=False, default=0)      # 批准后冻结、尚未清缴的配额
    deficit = Column(Numeric(18, 4), nullable=False, default=0)            # 缺口
    status = Column(String(16), nullable=False, default="pending")         # pending/compliant/deficit/reversed
    deadline = Column(String(10), nullable=False, default="")
    report_id = Column(Integer, ForeignKey("mrv_reports.id"), nullable=True, index=True)
    is_active = Column(Integer, nullable=False, default=1)                 # 0=报告冲正后归档，重新批准可建新记录
    idempotency_key = Column(String(64), nullable=True)  # 清缴请求去重键（全局唯一）
    cleared_at = Column(DateTime, nullable=True)


class TradeOrder(Base):
    """企业间配额交易订单：双方确认 → 卖方占用 → 交割划转 → 撤销释放。

    状态机：
    - pending：一方挂单，发起方默认已确认，等待对方确认（不占用任何配额）；
    - confirmed：双方均确认，卖方账户把对应数量从“可用”转为交易占用 reserved；
    - delivered：已交割，占用配额离开卖方持仓、买方入账，双方各留流水；
    - cancelled：交割前任一方撤销（或对方拒绝），释放卖方交易占用，不再可流转。
    """

    __tablename__ = "trade_orders"
    __table_args__ = (
        # 建单请求幂等：同一客户端重复提交（双击/重试）只生成一张订单。
        # NULL 不参与唯一约束，未携带幂等键的请求不受影响。
        UniqueConstraint("idempotency_key", name="uq_trade_order_idem"),
    )

    id = Column(Integer, primary_key=True)
    order_no = Column(String(32), nullable=False, unique=True, index=True)
    year = Column(Integer, nullable=False, index=True)
    seller_id = Column(Integer, ForeignKey("companies.id"), nullable=False, index=True)
    buyer_id = Column(Integer, ForeignKey("companies.id"), nullable=False, index=True)
    amount = Column(Numeric(18, 4), nullable=False)             # 交易量（tCO2）
    price = Column(Numeric(18, 2), nullable=False, default=0)   # 单价（元/t）
    status = Column(String(16), nullable=False, default="pending", index=True)  # pending/confirmed/delivered/cancelled
    seller_confirmed = Column(Integer, nullable=False, default=0)
    buyer_confirmed = Column(Integer, nullable=False, default=0)
    # 发起方：seller=卖方挂单，buyer=买方求购；用于建单时自动确认发起方
    initiator = Column(String(8), nullable=False, default="seller")
    tx_date = Column(String(10), nullable=False, default="")
    remark = Column(String(256), nullable=False, default="")
    cancel_reason = Column(String(256), nullable=False, default="")
    cancelled_by = Column(Integer, ForeignKey("companies.id"), nullable=True)
    idempotency_key = Column(String(64), nullable=True)
    # 交割闭环开关：交割时用买方到账配额自动核销其同年度履约缺口（默认开启）
    auto_clear_deficit = Column(Integer, nullable=False, default=1)
    confirmed_at = Column(DateTime, nullable=True)
    delivered_at = Column(DateTime, nullable=True)
    cancelled_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
