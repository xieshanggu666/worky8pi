"""统一账本事件溯源模型：事件流（append-only）、重放检查点与对账运行记录。

为什么在既有业务表（allowance_transactions / trade_orders / auction_trades …）
之外再建一层事件账：

- 五类业务（配额流水、企业订单、集中竞价、履约清缴、冲正回退）原先各自记各自的
  表，流水是“账户视角”，成交单/订单/履约是“业务视角”，两者之间只有松散的外键，
  没有一条能从头串到尾、可重放、可校验完整性的链；
- ``ledger_events`` 把每一次余额变动与业务状态流转统一为带业务来源（source/ref）、
  借贷方向、发生序号与链式哈希的不可变事件，旧记录通过迁移脚本回填（source=legacy），
  与新事件共用同一条链，天然“兼容旧记录”；
- ``ledger_checkpoints`` 保存每个账户重放到某一事件后的投影（余额/冻结/占用），
  对账与重放只需从最近检查点增量推进，不必每次全量回放；
- ``ledger_reconciliations`` 持久化每一次对账运行（含全部差异明细与系统守恒结果），
  本身也是可追溯的审计记录。

事件链只追加（append-only）：任何业务回退/冲正都不是“修改或删除旧事件”，而是
追加一笔方向相反的补偿事件，因此完整保留了“发生过什么 → 如何纠正”的审计轨迹，
这与业务层“冲正写反向流水、不删旧流水”的记账原则一致。
"""

from datetime import datetime

from sqlalchemy import (
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)

from app.core.database import Base


class LedgerEvent(Base):
    """统一账本事件：五类业务向账本投影的唯一事件源（append-only）。

    一条事件 = 一次对某账户年度账本的原子作用，或一次业务状态流转。
    事件按 ``seq`` 全局全序排列；同事务内多条事件由调用方保证连续登记，
    ``event_group`` 相同表示它们同生共死（同一业务事务），重放时整组生效。
    """

    __tablename__ = "ledger_events"
    __table_args__ = (
        # 同一业务来源只能登记一次：并发重试/双击经事件层再去重一道，
        # 与业务表自身的幂等键约束形成双保险。NULL 不参与唯一约束。
        UniqueConstraint("source", "source_ref", "occurrence", name="uq_ledger_event_source"),
        UniqueConstraint("seq", name="uq_ledger_event_seq"),
        Index("ix_ledger_event_account_seq", "account_id", "seq"),
        Index("ix_ledger_event_company_year", "company_id", "year"),
        Index("ix_ledger_event_group", "event_group"),
    )

    id = Column(Integer, primary_key=True)
    # 全局全序序号（同事务连续分配），重放即按 seq 升序
    seq = Column(Integer, nullable=False)
    # 同事务事件组（推荐用业务单号/幂等键），便于整组追溯
    event_group = Column(String(64), nullable=False, default="", index=True)
    # 业务来源：
    # quota_allocation/tx_transfer/trade_order/auction_bid/auction_trade/
    # compliance/report_reversal/auction_reversal/auction_repay/legacy
    source = Column(String(32), nullable=False, index=True)
    # 业务主键引用（如流水 id / 订单 id / 成交单 id / 履约记录 id）；
    # 回填的旧记录直接引用 allowance_transactions.id
    source_ref = Column(Integer, nullable=True)
    # 同一来源引用内的发生序号（一笔业务多条流水时区分，如成交结算的卖方出库+买方到账）
    occurrence = Column(Integer, nullable=False, default=0)
    # 兼容旧记录标记：1=由迁移脚本从既有业务表回填；0=登记器随业务实时写入
    is_legacy = Column(Integer, nullable=False, default=0)

    account_id = Column(Integer, ForeignKey("allowance_accounts.id"), nullable=True, index=True)
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=True, index=True)
    year = Column(Integer, nullable=True, index=True)
    # allocation/buy/sell/transfer_in/transfer_out/freeze/unfreeze/clear/…
    # 取值与 allowance_transactions.tx_type 对齐，另加 order/trade 状态事件类型
    event_type = Column(String(32), nullable=False)
    # 作用方向：debit（出账，持仓减少）/ credit（入账，持仓增加）/
    # reserve（增占用）/ release（降占用）/ freeze（增冻结）/ unfreeze（降冻结）/
    # status（纯业务状态流转，不动账）
    direction = Column(String(12), nullable=False, default="status")
    amount = Column(Numeric(18, 4), nullable=False, default=0)

    trade_order_id = Column(Integer, ForeignKey("trade_orders.id"), nullable=True, index=True)
    auction_trade_id = Column(Integer, ForeignKey("auction_trades.id"), nullable=True, index=True)
    compliance_record_id = Column(Integer, ForeignKey("compliance_records.id"), nullable=True, index=True)
    idempotency_key = Column(String(64), nullable=True, index=True)

    # 链式完整性：prev_hash = 上一条事件 hash，chain_hash 含本条内容载荷，
    # 任何插入/篡改/断序都会在重放校验时暴露（旧回填事件同样参与链）。
    prev_hash = Column(String(64), nullable=False, default="")
    chain_hash = Column(String(64), nullable=False, default="")
    payload = Column(Text, nullable=False, default="{}")  # 事件溯源附加快照（JSON）
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)
    # 业务实际发生时间（回填旧记录取流水 created_at），重放按 seq 而非此列
    occurred_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class LedgerCheckpoint(Base):
    """账户账本投影检查点：重放到 ``last_seq`` 后的（持仓/冻结/占用）。

    每个账户只保留一行（upsert）。重放器优先取最近检查点再增量应用后续事件；
    检查点投影可随时用全量重放重建（对账修复），不影响事件链本身。
    """

    __tablename__ = "ledger_checkpoints"
    __table_args__ = (
        UniqueConstraint("account_id", name="uq_ledger_checkpoint_account"),
    )

    id = Column(Integer, primary_key=True)
    account_id = Column(Integer, ForeignKey("allowance_accounts.id"), nullable=False, index=True)
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=True)
    year = Column(Integer, nullable=True)
    last_seq = Column(Integer, nullable=False, default=0)
    current_balance = Column(Numeric(18, 4), nullable=False, default=0)
    frozen_balance = Column(Numeric(18, 4), nullable=False, default=0)
    reserved_balance = Column(Numeric(18, 4), nullable=False, default=0)
    # 检查点时刻该账户事件链头部哈希（链式校验用）
    chain_hash = Column(String(64), nullable=False, default="")
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)


class LedgerReconciliation(Base):
    """对账运行记录：每次全量对账的参数、结论、差异明细与守恒校验结果。"""

    __tablename__ = "ledger_reconciliations"
    __table_args__ = (
        # 对账也支持幂等重试：同一幂等键的重复触发返回首次运行结果
        UniqueConstraint("idempotency_key", name="uq_ledger_recon_idem"),
        Index("ix_ledger_recon_status", "status"),
    )

    id = Column(Integer, primary_key=True)
    recon_no = Column(String(32), nullable=False, unique=True, index=True)
    scope = Column(String(16), nullable=False, default="full")  # full / company / year
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=True, index=True)
    year = Column(Integer, nullable=True, index=True)
    status = Column(String(16), nullable=False, default="running")  # running/balanced/discrepancy/failed
    checked_accounts = Column(Integer, nullable=False, default=0)
    checked_events = Column(Integer, nullable=False, default=0)
    discrepancy_count = Column(Integer, nullable=False, default=0)
    # 系统总配额守恒：期初+分配 ± 各类跨主体流转/清缴/冲正后的合计核对
    conserved = Column(Integer, nullable=False, default=1)
    discrepancies_json = Column(Text, nullable=False, default="[]")
    summary_json = Column(Text, nullable=False, default="{}")
    idempotency_key = Column(String(64), nullable=True)
    triggered_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    started_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    finished_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
