from datetime import datetime

from sqlalchemy import (
    Column,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
)

from app.core.database import Base


class QuotaLoan(Base):
    """配额借贷单：出借企业与借入企业协同完成「申请 → 双方确认 → 冻结 → 放款 →
    到期归还 →（逾期/违约 → 监管追偿）」的全生命周期。

    状态机：
    - pending：一方发起（发起方默认已确认），等待对方确认，此阶段不占用任何配额；
    - confirmed：双方确认，出借方对应数量从“自由可用”转为交易占用 reserved，
      与企业间订单/竞价占用共用同一套账本隔离不变量；
    - active：已放款。占用配额离开出借方持仓（current/reserved 同减）、借入方
      到账（current 同增），默认在同一事务内自动核销借入方同年度履约缺口；
    - overdue：到达到期日仍未足额归还，由监管巡检/手动标记；
    - defaulted：监管对逾期借贷宣布违约，登记违约欠额快照，待追偿；
    - repaid：已足额归还（含正常归还、逾期归还与违约追偿结清），终态；
    - cancelled：放款前任一方撤销（confirmed 撤销释放占用），终态。
    """

    __tablename__ = "quota_loans"
    __table_args__ = (
        # 建单请求幂等：双击/超时重试只生成一张借贷单（NULL 不参与唯一约束）
        UniqueConstraint("idempotency_key", name="uq_quota_loan_idem"),
    )

    id = Column(Integer, primary_key=True)
    loan_no = Column(String(32), nullable=False, unique=True, index=True)
    year = Column(Integer, nullable=False, index=True)
    lender_id = Column(Integer, ForeignKey("companies.id"), nullable=False, index=True)
    borrower_id = Column(Integer, ForeignKey("companies.id"), nullable=False, index=True)
    amount = Column(Numeric(18, 4), nullable=False)              # 借贷数量（tCO2）
    price = Column(Numeric(18, 2), nullable=False, default=0)    # 约定费用单价（元/t，仅登记，配额清偿不涉及资金划转）
    # pending/confirmed/active/overdue/defaulted/repaid/cancelled
    status = Column(String(16), nullable=False, default="pending", index=True)
    lender_confirmed = Column(Integer, nullable=False, default=0)
    borrower_confirmed = Column(Integer, nullable=False, default=0)
    # 发起方：lender=出借方挂出 / borrower=借入方求借；建单即视为发起方已确认
    initiator = Column(String(8), nullable=False, default="lender")
    due_date = Column(String(10), nullable=False, default="")    # 到期清偿日（YYYY-MM-DD）
    disbursed_amount = Column(Numeric(18, 4), nullable=False, default=0)
    repaid_amount = Column(Numeric(18, 4), nullable=False, default=0)
    # 宣布违约时未偿余额快照（随后的追偿只增 repaid_amount，不回改本快照）
    defaulted_amount = Column(Numeric(18, 4), nullable=False, default=0)
    # 放款到账是否自动核销借入方同年度履约缺口（默认开启，年度配额闭环）
    auto_clear_deficit = Column(Integer, nullable=False, default=1)
    # 借入方后续交易/竞价到账时，是否自动用其自由可用配额追偿逾期/违约欠额（默认开启）
    auto_recover_default = Column(Integer, nullable=False, default=1)
    tx_date = Column(String(10), nullable=False, default="")
    remark = Column(String(256), nullable=False, default="")
    cancel_reason = Column(String(256), nullable=False, default="")
    cancelled_by = Column(Integer, ForeignKey("companies.id"), nullable=True)
    default_reason = Column(String(500), nullable=False, default="")
    idempotency_key = Column(String(64), nullable=True)
    confirmed_at = Column(DateTime, nullable=True)
    disbursed_at = Column(DateTime, nullable=True)
    overdue_at = Column(DateTime, nullable=True)
    defaulted_at = Column(DateTime, nullable=True)
    repaid_at = Column(DateTime, nullable=True)
    cancelled_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class QuotaLoanRepayment(Base):
    """借贷归还/追偿流水凭据：借入方自由可用配额划出、出借方到账。

    每笔归还/追偿一行；同一借贷单可分次部分归还，各行 quantity 累计不超过
    借贷量。``kind`` 区分正常归还（normal）、逾期归还（overdue）与违约追偿
    （recover，含监管手动与到账自动追偿）。
    """

    __tablename__ = "quota_loan_repayments"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_quota_loan_repay_idem"),
    )

    id = Column(Integer, primary_key=True)
    repay_no = Column(String(32), nullable=False, unique=True, index=True)
    loan_id = Column(Integer, ForeignKey("quota_loans.id"), nullable=False, index=True)
    lender_id = Column(Integer, ForeignKey("companies.id"), nullable=False, index=True)
    borrower_id = Column(Integer, ForeignKey("companies.id"), nullable=False, index=True)
    year = Column(Integer, nullable=False, index=True)
    quantity = Column(Numeric(18, 4), nullable=False)
    # normal=到期前/到期正常归还；overdue=逾期后手动归还；recover=违约/逾期追偿
    kind = Column(String(16), nullable=False, default="normal")
    # manual=借入方/监管手动触发；auto=后续交易到账自动追偿
    source = Column(String(16), nullable=False, default="manual")
    operator_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    remark = Column(String(256), nullable=False, default="")
    idempotency_key = Column(String(64), nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class QuotaLoanAuditLog(Base):
    """配额借贷权限与操作审计：建单/确认/撤销/放款/归还/逾期/违约/追偿与越权拒绝均留痕。"""

    __tablename__ = "quota_loan_audit_logs"

    id = Column(Integer, primary_key=True)
    operator_id = Column(Integer, nullable=True, index=True)
    operator_name = Column(String(64), nullable=False, default="")
    operator_role = Column(String(16), nullable=False, default="")
    # loan.create/confirm/cancel/disburse/repay/overdue.scan/overdue.mark/default/recover
    # 以及 loan.auto_recover、access.denied
    action = Column(String(64), nullable=False, index=True)
    target_type = Column(String(16), nullable=False, default="")
    target_id = Column(Integer, nullable=True)
    loan_id = Column(Integer, nullable=True, index=True)
    detail = Column(String(500), nullable=False, default="")
    result = Column(String(16), nullable=False, default="success")  # success / denied
    ip = Column(String(64), nullable=False, default="")
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
