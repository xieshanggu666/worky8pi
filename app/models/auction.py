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


class AuctionSession(Base):
    """碳配额集中竞价场次：监管创建 → 开放报价 → 统一撮合 → 集中结算。

    状态机：
    - draft：草稿，监管可编辑公告信息（保留价、品种、时间窗），企业不可见报价入口；
    - open：报价开放，买/卖企业提交密封报价，可撤单；
    - matched：已撮合。按统一出清价生成成交单，卖方对应配额转为交易占用 reserved；
    - settled：已结算。占用配额离开卖方、买方到账，同事务回写流水并核销买方履约缺口；
    - cancelled：草稿/开放期撤场（无账本副作用），或撮合后撤场（逐笔释放卖方占用）。
    """

    __tablename__ = "auction_sessions"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_auction_session_idem"),
    )

    id = Column(Integer, primary_key=True)
    session_no = Column(String(32), nullable=False, unique=True, index=True)
    name = Column(String(128), nullable=False, default="")
    year = Column(Integer, nullable=False, index=True)
    product = Column(String(32), nullable=False, default="allowance")  # 配额品种（预留：allowance/CCER）
    reserve_price = Column(Numeric(18, 2), nullable=False, default=0)  # 保留价：低于该价的卖出不参与撮合
    estimated_volume = Column(Numeric(18, 4), nullable=True)           # 公告拟成交量（仅展示）
    status = Column(String(16), nullable=False, default="draft", index=True)
    clear_price = Column(Numeric(18, 2), nullable=True)                # 撮合成交统一价
    matched_volume = Column(Numeric(18, 4), nullable=False, default=0)
    trade_count = Column(Integer, nullable=False, default=0)
    # 结算时是否用买方到账配额自动核销其同年度履约缺口（默认开启，年度配额闭环）
    auto_clear_deficit = Column(Integer, nullable=False, default=1)
    # 后续场次结算到账时，是否自动用买方自由可用配额追偿其历史违约欠额（默认开启）
    auto_recover_default = Column(Integer, nullable=False, default=1)
    open_at = Column(DateTime, nullable=True)
    close_at = Column(DateTime, nullable=True)
    matched_at = Column(DateTime, nullable=True)
    settled_at = Column(DateTime, nullable=True)
    cancelled_at = Column(DateTime, nullable=True)
    cancel_reason = Column(String(256), nullable=False, default="")
    cancelled_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    remark = Column(String(256), nullable=False, default="")
    idempotency_key = Column(String(64), nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class AuctionBid(Base):
    """集中竞价报价单：买方（求购）/卖方（出让）在开放场次内密封报价。

    状态：
    - active：有效报价，开放期可撤；卖方报量不得超过自由可用配额；
    - matched：撮合成交量等于报价量（全部成交）；
    - partial：部分成交，余量不再参与后续撮合（本场次单次撮合）；
    - unmatched：撮合后未成交（价量不满足出清条件），终态；
    - cancelled：开放期撤单、监管撤单或场次取消，终态。
    """

    __tablename__ = "auction_bids"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_auction_bid_idem"),
        # 同一企业在同一场次同一方向只允许一张有效报价；撤单/成交后该约束自然释放，
        # 允许重新报价。部分唯一索引在 SQLite/PostgreSQL 生效，其他库由场次键锁兜底。
        Index(
            "uq_auction_active_bid",
            "session_id",
            "company_id",
            "side",
            unique=True,
            sqlite_where=text("status = 'active'"),
            postgresql_where=text("status = 'active'"),
        ),
    )

    id = Column(Integer, primary_key=True)
    bid_no = Column(String(32), nullable=False, unique=True, index=True)
    session_id = Column(Integer, ForeignKey("auction_sessions.id"), nullable=False, index=True)
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=False, index=True)
    side = Column(String(4), nullable=False)  # buy / sell
    year = Column(Integer, nullable=False, index=True)
    quantity = Column(Numeric(18, 4), nullable=False)
    price = Column(Numeric(18, 2), nullable=False, default=0)
    filled_quantity = Column(Numeric(18, 4), nullable=False, default=0)
    # active/matched/partial/unmatched/cancelled
    status = Column(String(16), nullable=False, default="active", index=True)
    tx_date = Column(String(10), nullable=False, default="")
    remark = Column(String(256), nullable=False, default="")
    cancel_reason = Column(String(256), nullable=False, default="")
    cancelled_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    idempotency_key = Column(String(64), nullable=True)
    matched_at = Column(DateTime, nullable=True)
    cancelled_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class AuctionTrade(Base):
    """竞价成交单：撮合时按出清价生成并占用卖方配额，结算时双方账户划转。

    状态：
    - reserved：撮合完成，卖方配额已转为交易占用，等待场次统一结算；
    - settled：已结算，卖方出库 / 买方到账 / 履约缺口核销全部落库；
    - reversed：监管冲正完成，结算划转已回退（含联动清缴回滚），
      无违约敞口；
    - defaulted：冲正时买方持仓不足，部分配额无法收回，登记违约欠额，
      待买方补缴追偿；补缴结清后自动转为 reversed；
    - cancelled：撮合后场次被监管撤销，占用已释放，成交单作废。

    注：已 settled 的成交单可能仅被部分冲正（监管指定的数量小于成交量），
    此时状态保持 settled，已冲正量累计在 ``reversed_quantity``；仅在整笔
    成交单被完全冲正（reversed_quantity == quantity）时才离开 settled。
    """

    __tablename__ = "auction_trades"

    id = Column(Integer, primary_key=True)
    trade_no = Column(String(32), nullable=False, unique=True, index=True)
    session_id = Column(Integer, ForeignKey("auction_sessions.id"), nullable=False, index=True)
    buyer_bid_id = Column(Integer, ForeignKey("auction_bids.id"), nullable=False, index=True)
    seller_bid_id = Column(Integer, ForeignKey("auction_bids.id"), nullable=False, index=True)
    buyer_id = Column(Integer, ForeignKey("companies.id"), nullable=False, index=True)
    seller_id = Column(Integer, ForeignKey("companies.id"), nullable=False, index=True)
    year = Column(Integer, nullable=False, index=True)
    quantity = Column(Numeric(18, 4), nullable=False)
    price = Column(Numeric(18, 2), nullable=False, default=0)  # 统一出清价
    alloc_seq = Column(Integer, nullable=False, default=0)     # 价格-时间优先撮合序号
    # reserved/settled/reversed/defaulted/cancelled
    status = Column(String(16), nullable=False, default="reserved", index=True)
    settled_at = Column(DateTime, nullable=True)
    # 监管冲正累计回退量（≤ quantity）；defaulted_amount 为买方无法收回、待追偿的欠额
    reversed_quantity = Column(Numeric(18, 4), nullable=False, default=0)
    defaulted_amount = Column(Numeric(18, 4), nullable=False, default=0)
    repaid_amount = Column(Numeric(18, 4), nullable=False, default=0)
    cancelled_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class AuctionReversalBatch(Base):
    """监管对已结算场次的冲正批次：一次操作可覆盖多笔成交单。

    批次与批次内全部冲正单、账户/流水/履约回退在同一事务提交（同生共死）。
    ``idempotency_key`` 唯一约束保证双击/超时重试只生效一次。
    """

    __tablename__ = "auction_reversal_batches"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_auction_reversal_batch_idem"),
    )

    id = Column(Integer, primary_key=True)
    batch_no = Column(String(32), nullable=False, unique=True, index=True)
    session_id = Column(Integer, ForeignKey("auction_sessions.id"), nullable=False, index=True)
    reason = Column(String(500), nullable=False, default="")
    operator_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    trade_count = Column(Integer, nullable=False, default=0)       # 批次内冲正单笔数
    reverse_volume = Column(Numeric(18, 4), nullable=False, default=0)  # 本次回退配额合计
    recovered_volume = Column(Numeric(18, 4), nullable=False, default=0)  # 自买方收回并退还卖方
    default_volume = Column(Numeric(18, 4), nullable=False, default=0)    # 买方违约欠额合计
    idempotency_key = Column(String(64), nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class AuctionTradeReversal(Base):
    """单笔成交单的冲正明细：配额回退、清缴回滚与违约登记的审计凭据。

    每笔成交单可被分多次部分冲正（每次一行），各行 reverse_quantity 之和
    不超过成交单量；由批次事务 + 成交单行锁串行化，并发冲正不会超额回退。
    """

    __tablename__ = "auction_trade_reversals"

    id = Column(Integer, primary_key=True)
    reversal_no = Column(String(32), nullable=False, unique=True, index=True)
    batch_id = Column(Integer, ForeignKey("auction_reversal_batches.id"), nullable=False, index=True)
    session_id = Column(Integer, ForeignKey("auction_sessions.id"), nullable=False, index=True)
    trade_id = Column(Integer, ForeignKey("auction_trades.id"), nullable=False, index=True)
    buyer_id = Column(Integer, ForeignKey("companies.id"), nullable=False, index=True)
    seller_id = Column(Integer, ForeignKey("companies.id"), nullable=False, index=True)
    year = Column(Integer, nullable=False, index=True)
    quantity = Column(Numeric(18, 4), nullable=False)             # 本次申请回退量
    # 结算联动清缴回滚：解除冻结 f 吨、回退已补缴自由配额 c 吨
    clear_unfrozen = Column(Numeric(18, 4), nullable=False, default=0)
    clear_refunded = Column(Numeric(18, 4), nullable=False, default=0)
    # 自买方自由可用实际收回（f+c 中拿得回的部分）；不足部分登记违约欠额
    recovered_quantity = Column(Numeric(18, 4), nullable=False, default=0)
    defaulted_quantity = Column(Numeric(18, 4), nullable=False, default=0)
    reason = Column(String(500), nullable=False, default="")
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class AuctionDefaultRepayment(Base):
    """买方违约欠额补缴/追偿：买方补足配额后划付受影响卖方并解除违约。

    可由监管手动触发，或在后续场次结算（买方有配额到账）时自动追偿。
    每笔成交单每次追偿一行；``repaid_quantity`` 累计达到欠额时，
    成交单由 defaulted 回到 reversed。
    """

    __tablename__ = "auction_default_repayments"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_auction_default_repay_idem"),
    )

    id = Column(Integer, primary_key=True)
    repay_no = Column(String(32), nullable=False, unique=True, index=True)
    session_id = Column(Integer, ForeignKey("auction_sessions.id"), nullable=False, index=True)
    trade_id = Column(Integer, ForeignKey("auction_trades.id"), nullable=False, index=True)
    reversal_id = Column(Integer, ForeignKey("auction_trade_reversals.id"), nullable=True, index=True)
    buyer_id = Column(Integer, ForeignKey("companies.id"), nullable=False, index=True)
    seller_id = Column(Integer, ForeignKey("companies.id"), nullable=False, index=True)
    year = Column(Integer, nullable=False, index=True)
    quantity = Column(Numeric(18, 4), nullable=False)             # 本次追偿量
    # auto=后续结算到账自动追偿；manual=监管手动触发
    source = Column(String(16), nullable=False, default="manual")
    operator_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    remark = Column(String(256), nullable=False, default="")
    idempotency_key = Column(String(64), nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class AuctionAuditLog(Base):
    """竞价市场权限与操作审计：场次管理、报价/撤单、撮合结算、越权拒绝均留痕。"""

    __tablename__ = "auction_audit_logs"

    id = Column(Integer, primary_key=True)
    operator_id = Column(Integer, nullable=True, index=True)
    operator_name = Column(String(64), nullable=False, default="")
    operator_role = Column(String(16), nullable=False, default="")
    # session.create/open/match/settle/cancel、bid.place/cancel、access.denied
    action = Column(String(64), nullable=False, index=True)
    # session / bid / trade
    target_type = Column(String(16), nullable=False, default="")
    target_id = Column(Integer, nullable=True)
    session_id = Column(Integer, nullable=True, index=True)
    detail = Column(String(500), nullable=False, default="")
    result = Column(String(16), nullable=False, default="success")  # success / denied
    ip = Column(String(64), nullable=False, default="")
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
