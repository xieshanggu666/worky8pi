"""配额借贷（企业间配额拆借）模型：借据、归还/追偿流水与权限审计。

业务状态机见 :mod:`app.services.borrowing_service`。简表::

    pending ──双方确认──▶ pending（仅确认标志变化）
    pending ──监管批准（双方已确认）──▶ reserved（出借方自由可用 → 借贷冻结 reserved）
    pending ──监管驳回 / 任一方撤销 / 监管撤销──▶ rejected / cancelled
    reserved ──监管拨付──▶ active（冻结配额出库、借入方到账，可同事务核销履约缺口）
    reserved ──监管撤销──▶ cancelled（释放冻结）
    active  ──到期日届满监管标记──▶ overdue
    active/overdue ──足额归还──▶ repaid
    overdue ──监管宣告违约（尽力收回后仍有欠额）──▶ defaulted ──追偿结清──▶ repaid
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
    UniqueConstraint,
)

from app.core.database import Base


class QuotaLoan(Base):
    """企业间配额拆借借据：双方协商 → 监管冻结/拨付 → 到期归还 → 违约追偿。

    状态：
    - pending：一方发起（发起方默认已确认），等待对方确认与监管批准，不动账；
    - reserved：双方已确认且监管批准，出借方对应数量的自由可用配额转为
      “借贷冻结”（复用账户 reserved_balance，与交易占用同一套
      ``current ≥ frozen + reserved`` 原子约束），等待监管拨付；
    - active：监管已拨付，冻结配额离开出借方持仓、借入方到账，
      借据生效中，借入方可分次归还；
    - overdue：已过约定归还日但未结清，监管标记，仍可正常归还；
    - defaulted：监管对逾期借据宣告违约，已尽力收回借入方自由可用配额，
      不足部分登记为违约欠额，待后续追偿；追偿结清后回到 repaid；
    - repaid：已全部归还（含违约欠额追偿结清），终态；
    - rejected/cancelled：监管驳回（pending）/ 任一方或监管撤销
      （pending 无账本副作用；reserved 撤销释放冻结），终态。
    """

    __tablename__ = "quota_loans"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_quota_loan_idem"),
        Index("ix_quota_loan_party_year", "borrower_id", "year"),
    )

    id = Column(Integer, primary_key=True)
    loan_no = Column(String(32), nullable=False, unique=True, index=True)
    year = Column(Integer, nullable=False, index=True)
    lender_id = Column(Integer, ForeignKey("companies.id"), nullable=False, index=True)
    borrower_id = Column(Integer, ForeignKey("companies.id"), nullable=False, index=True)
    amount = Column(Numeric(18, 4), nullable=False)                # 拆借本金（tCO2）
    interest_rate = Column(Numeric(8, 4), nullable=False, default=0)  # 约定费率（%，资金侧自行结算）
    due_date = Column(String(10), nullable=False, default="")      # 约定归还日 YYYY-MM-DD
    status = Column(String(16), nullable=False, default="pending", index=True)

    lender_confirmed = Column(Integer, nullable=False, default=0)
    borrower_confirmed = Column(Integer, nullable=False, default=0)
    # 发起方：lender=出借方挂出 / borrower=借入方求借，发起方建单即确认
    initiator = Column(String(8), nullable=False, default="lender")

    # 已归还给出借方的累计数量（含违约宣告时的尽力收回与随后追偿）
    returned_amount = Column(Numeric(18, 4), nullable=False, default=0)
    # 宣告违约时无法收回、登记在案的欠额；recovered_default 为其后已追偿量
    defaulted_amount = Column(Numeric(18, 4), nullable=False, default=0)
    recovered_default = Column(Numeric(18, 4), nullable=False, default=0)

    # 拨付到账时是否用借入方到账配额自动核销其同年度履约缺口（默认开启）
    auto_clear_deficit = Column(Integer, nullable=False, default=1)
    # 借入方后续因交易/竞价到账时是否自动追偿其违约欠额（默认开启）
    auto_recover_default = Column(Integer, nullable=False, default=1)

    tx_date = Column(String(10), nullable=False, default="")
    remark = Column(String(256), nullable=False, default="")
    reject_reason = Column(String(256), nullable=False, default="")
    cancel_reason = Column(String(256), nullable=False, default="")
    cancelled_by = Column(Integer, nullable=True)  # 撤销方企业 id（企业撤销）或 0（监管撤销）

    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    approved_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    rejected_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    disbursed_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    defaulted_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    idempotency_key = Column(String(64), nullable=True)

    confirmed_at = Column(DateTime, nullable=True)
    reserved_at = Column(DateTime, nullable=True)
    rejected_at = Column(DateTime, nullable=True)
    disbursed_at = Column(DateTime, nullable=True)
    overdue_at = Column(DateTime, nullable=True)
    defaulted_at = Column(DateTime, nullable=True)
    closed_at = Column(DateTime, nullable=True)
    cancelled_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class QuotaLoanRepayment(Base):
    """借据归还/追偿明细：正常归还、违约宣告时尽力收回与后续违约追偿逐笔留痕。

    ``source`` 取值：
    - repay：借入方到期前/逾期后主动归还（normal）；
    - default_clawback：监管宣告违约时自借入方自由可用尽力收回；
    - manual_recovery：监管对违约欠额手动追偿；
    - auto_recovery：借入方后续交易/竞价配额到账时同事务自动追偿。

    借据全部归还/追偿流水均为跨主体配对（借入方出库 / 出借方到账），
    合计与借据 ``returned_amount`` 勾稽，是对账“单据↔账本”的依据。
    """

    __tablename__ = "quota_loan_repayments"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_quota_loan_repay_idem"),
    )

    id = Column(Integer, primary_key=True)
    repay_no = Column(String(32), nullable=False, unique=True, index=True)
    loan_id = Column(Integer, ForeignKey("quota_loans.id"), nullable=False, index=True)
    year = Column(Integer, nullable=False, index=True)
    lender_id = Column(Integer, ForeignKey("companies.id"), nullable=False, index=True)
    borrower_id = Column(Integer, ForeignKey("companies.id"), nullable=False, index=True)
    quantity = Column(Numeric(18, 4), nullable=False)
    # repay/default_clawback/manual_recovery/auto_recovery
    source = Column(String(20), nullable=False, default="repay")
    operator_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    remark = Column(String(256), nullable=False, default="")
    idempotency_key = Column(String(64), nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class QuotaLoanAuditLog(Base):
    """配额借贷权限与操作审计：协商、批准/冻结、拨付、归还、违约追偿与越权拒绝均留痕。"""

    __tablename__ = "quota_loan_audit_logs"

    id = Column(Integer, primary_key=True)
    operator_id = Column(Integer, nullable=True, index=True)
    operator_name = Column(String(64), nullable=False, default="")
    operator_role = Column(String(16), nullable=False, default="")
    # loan.create/confirm/cancel/approve/reject/disburse/repay/overdue/default/recover、
    # access.denied
    action = Column(String(64), nullable=False, index=True)
    target_type = Column(String(16), nullable=False, default="loan")
    target_id = Column(Integer, nullable=True)
    loan_id = Column(Integer, nullable=True, index=True)
    detail = Column(String(500), nullable=False, default="")
    result = Column(String(16), nullable=False, default="success")  # success / denied
    ip = Column(String(64), nullable=False, default="")
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
