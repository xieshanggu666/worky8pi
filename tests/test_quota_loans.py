"""配额借贷与到期清偿服务层测试。

覆盖：
- 状态机主线：建单（发起方即确认）→ 对方确认（出借配额转交易占用）→
  放款（占用出库/借入到账，同事务核销借入方履约缺口）→ 到期归还（分次/足额）→
  逾期标记 → 宣布违约 → 手动/自动追偿结清；
- 撤销：pending 撤销无账本副作用，confirmed 撤销释放占用；
- 账本隔离：出借占用与履约冻结、其他订单占用互不挤占；
- 借入方放款到账自动清缴（先冻结、后到账补缴，loan_deficit_clear 关联借贷单），
  关闭 auto_clear_deficit 不联动；
- 余额不足拒绝、非参与方拒绝、非法状态流转拒绝、重复请求幂等；
- 多线程并发放款/归还/确认只生效一次，四类余额与流水一致、系统总配额守恒；
- 订单交割/竞价结算到账后在清缴之后自动追偿逾期/违约借贷欠额。
"""

from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
from app.core.event_hooks import install_ledger_hooks
from app.models import (
    AllowanceAccount,
    AllowanceTransaction,
    CalculationMethod,
    Company,
    ComplianceRecord,
    EmissionFactor,
    EmissionScope,
    QuotaLoan,
    QuotaLoanRepayment,
)
from app.services.loan_service import (
    ACTIVE,
    CANCELLED,
    CONFIRMED,
    DEFAULTED,
    OVERDUE,
    PENDING,
    REPAID,
    LoanError,
    cancel_loan,
    confirm_loan,
    create_loan,
    declare_default,
    disburse_loan,
    mark_overdue_loans,
    recover_borrower_loans,
    repay_loan,
)
from app.services.mrv_service import approve_report, generate_report, submit_report
from app.services.quota_service import allocate_quota
from app.services.trade_order_service import (
    confirm_order,
    create_order,
    deliver_order,
)

YEAR = 2026
FACTOR = 0.5703


@pytest.fixture()
def db(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'loan.db'}",
        connect_args={"check_same_thread": False, "timeout": 30},
    )

    @event.listens_for(engine, "connect")
    def _busy_timeout(dbapi_conn, _rec):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA busy_timeout=30000")
        cur.close()

    Base.metadata.create_all(engine)
    TestSession = sessionmaker(bind=engine, autoflush=False)
    install_ledger_hooks(TestSession)
    session = TestSession()
    yield session
    session.close()
    engine.dispose()


@pytest.fixture()
def companies(db):
    """出借方 1000 吨、借入方 200 吨、第三方 100 吨。"""
    lender = Company(code="LN-001", name="出借企业", industry="电力", region="华东")
    borrower = Company(code="BR-001", name="借入企业", industry="水泥", region="华北")
    third = Company(code="TH-001", name="第三方企业", industry="化工", region="华南")
    db.add_all([lender, borrower, third])
    db.flush()
    allocate_quota(db, lender.id, YEAR, baseline=1000, allocation_amount=1000)
    allocate_quota(db, borrower.id, YEAR, baseline=200, allocation_amount=200)
    allocate_quota(db, third.id, YEAR, baseline=100, allocation_amount=100)
    db.commit()
    db.expire_all()
    return {"lender": lender, "borrower": borrower, "third": third}


def _account(db, company_id):
    return db.query(AllowanceAccount).filter_by(company_id=company_id, year=YEAR).one()


def _make_loan(db, companies, amount=100, initiator="lender", **kw):
    loan = create_loan(
        db,
        companies["lender"].id,
        companies["borrower"].id,
        YEAR,
        amount,
        price=kw.get("price", 10.0),
        initiator=initiator,
        due_date=kw.get("due_date", f"{YEAR}-12-31"),
        idempotency_key=kw.get("idempotency_key"),
        auto_clear_deficit=kw.get("auto_clear_deficit", True),
        auto_recover_default=kw.get("auto_recover_default", True),
    )
    db.commit()
    return loan


def _confirm_and_disburse(db, loan):
    loan = db.get(QuotaLoan, loan.id)
    other = loan.borrower_id if loan.initiator == "lender" else loan.lender_id
    confirm_loan(db, loan.id, other)
    db.commit()
    disburse_loan(db, loan.id, loan.lender_id)
    db.commit()
    return db.get(QuotaLoan, loan.id)


# --------------------------------------------------------------------------- #
# 建单 / 确认 / 冻结
# --------------------------------------------------------------------------- #

class TestCreateAndConfirm:
    def test_create_initiator_confirmed(self, db, companies):
        loan = _make_loan(db, companies, 100)
        assert loan.status == PENDING
        assert loan.lender_confirmed == 1
        assert loan.borrower_confirmed == 0

    def test_borrower_initiated(self, db, companies):
        loan = _make_loan(db, companies, 100, initiator="borrower")
        assert loan.borrower_confirmed == 1
        assert loan.lender_confirmed == 0

    def test_same_party_rejected(self, db, companies):
        with pytest.raises(LoanError):
            create_loan(
                db, companies["lender"].id, companies["lender"].id,
                YEAR, 100, due_date=f"{YEAR}-12-31",
            )

    def test_bad_amount_and_due_date(self, db, companies):
        with pytest.raises(LoanError):
            create_loan(db, companies["lender"].id, companies["borrower"].id,
                        YEAR, 0, due_date=f"{YEAR}-12-31")
        with pytest.raises(LoanError):
            create_loan(db, companies["lender"].id, companies["borrower"].id,
                        YEAR, 100, due_date="not-a-date")

    def test_non_party_confirm_rejected(self, db, companies):
        loan = _make_loan(db, companies, 100)
        with pytest.raises(LoanError):
            confirm_loan(db, loan.id, companies["third"].id)

    def test_second_confirmation_reserves_lender_free(self, db, companies):
        loan = _make_loan(db, companies, 100)
        lender_acc = _account(db, companies["lender"].id)
        assert float(lender_acc.reserved_balance) == 0

        confirm_loan(db, loan.id, companies["borrower"].id)
        db.commit()
        db.refresh(lender_acc)
        assert float(lender_acc.current_balance) == 1000
        assert float(lender_acc.reserved_balance) == 100  # 持仓不变，占用 +100
        assert db.get(QuotaLoan, loan.id).status == CONFIRMED

        # 重复确认幂等
        confirm_loan(db, loan.id, companies["borrower"].id)
        db.commit()
        db.refresh(lender_acc)
        assert float(lender_acc.reserved_balance) == 100

    def test_reserve_rejected_when_lender_free_insufficient(self, db, companies):
        # 出借方 1000 全部被另一张已确认借贷占用后，再确认第二张应拒绝
        first = _make_loan(db, companies, 1000)
        confirm_loan(db, first.id, companies["borrower"].id)
        db.commit()
        second = _make_loan(db, companies, 100)
        with pytest.raises(LoanError):
            confirm_loan(db, second.id, companies["borrower"].id)
        db.rollback()
        # 拒绝后占用未增加、单据仍 pending
        lender_acc = _account(db, companies["lender"].id)
        assert float(lender_acc.reserved_balance) == 1000
        assert db.get(QuotaLoan, second.id).status == PENDING

    def test_create_idempotent(self, db, companies):
        kw = dict(
            price=10, initiator="lender", due_date=f"{YEAR}-12-31",
            idempotency_key="idem-create-1",
        )
        l1 = create_loan(db, companies["lender"].id, companies["borrower"].id,
                         YEAR, 100, **kw)
        db.commit()
        l2 = create_loan(db, companies["lender"].id, companies["borrower"].id,
                         YEAR, 100, **kw)
        db.commit()
        assert l1.id == l2.id
        assert db.query(QuotaLoan).count() == 1


# --------------------------------------------------------------------------- #
# 撤销（冻结释放）
# --------------------------------------------------------------------------- #

class TestCancel:
    def test_cancel_pending_no_ledger_effect(self, db, companies):
        loan = _make_loan(db, companies, 100)
        cancel_loan(db, loan.id, companies["lender"].id, reason="暂不出借")
        db.commit()
        assert db.get(QuotaLoan, loan.id).status == CANCELLED
        lender_acc = _account(db, companies["lender"].id)
        assert float(lender_acc.reserved_balance) == 0

    def test_cancel_confirmed_releases_reserve(self, db, companies):
        loan = _make_loan(db, companies, 100)
        confirm_loan(db, loan.id, companies["borrower"].id)
        db.commit()
        cancel_loan(db, loan.id, companies["borrower"].id)
        db.commit()
        lender_acc = _account(db, companies["lender"].id)
        assert float(lender_acc.current_balance) == 1000
        assert float(lender_acc.reserved_balance) == 0
        # 重复撤销幂等
        cancel_loan(db, loan.id, companies["borrower"].id)
        db.commit()
        assert float(lender_acc.reserved_balance) == 0

    def test_non_party_cancel_rejected(self, db, companies):
        loan = _make_loan(db, companies, 100)
        with pytest.raises(LoanError):
            cancel_loan(db, loan.id, companies["third"].id)


# --------------------------------------------------------------------------- #
# 放款 + 年度履约联动
# --------------------------------------------------------------------------- #

def _setup_borrower_deficit(db, companies, emission):
    """构造借入方年度缺口：边界/因子/方法 → 活动数据 → 核算 → 报告批准。"""
    borrower = companies["borrower"]
    db.add(EmissionScope(company_id=borrower.id, scope="2",
                         category="外购电力", name="厂区用电"))
    db.add(CalculationMethod(
        method_code="ELEC", name="外购电力排放因子法", scope="2",
        formula_type="activity_factor"))
    db.add(EmissionFactor(
        factor_code="ELEC-GRID", name="外购电力", scope="2", unit="tCO2/MWh",
        value=FACTOR, source="电网因子", valid_from=f"{YEAR}-01-01",
        valid_to=f"{YEAR}-12-31"))
    db.flush()
    from app.models import ActivityData
    from app.services.calculation_service import recalc_company_year

    qty = emission / FACTOR
    scope = db.query(EmissionScope).filter_by(company_id=borrower.id, scope="2").one()
    db.add(ActivityData(
        company_id=borrower.id, scope_id=scope.id, year=YEAR, period="yearly",
        activity_type="外购电力", unit="MWh", quantity=qty, data_source="电网单",
        verified=1,
    ))
    db.commit()
    recalc_company_year(db, borrower.id, YEAR)
    report = generate_report(db, borrower.id, YEAR)
    submit_report(db, report)
    approve_report(db, report, verifier_id=1)
    db.commit()
    return db.query(ComplianceRecord).filter_by(
        company_id=borrower.id, year=YEAR, is_active=1).one()


class TestDisburse:
    def test_disburse_moves_quota(self, db, companies):
        loan = _confirm_and_disburse(db, _make_loan(db, companies, 100))
        assert loan.status == ACTIVE
        assert float(loan.disbursed_amount) == 100
        lender_acc = _account(db, companies["lender"].id)
        borrower_acc = _account(db, companies["borrower"].id)
        assert float(lender_acc.current_balance) == 900
        assert float(lender_acc.reserved_balance) == 0
        assert float(borrower_acc.current_balance) == 300

    def test_disburse_before_confirm_rejected(self, db, companies):
        loan = _make_loan(db, companies, 100)
        with pytest.raises(LoanError):
            disburse_loan(db, loan.id, companies["lender"].id)

    def test_disburse_rejected_by_third_party(self, db, companies):
        loan = _make_loan(db, companies, 100)
        with pytest.raises(LoanError):
            disburse_loan(db, loan.id, companies["third"].id)

    def test_disburse_auto_clears_borrower_deficit(self, db, companies):
        # 借入方排放 300：自有 200（冻结后仍缺口 100），借入 150 到账即补缴
        record = _setup_borrower_deficit(db, companies, 300)
        assert float(record.deficit) == pytest.approx(100, abs=0.01)

        loan = _confirm_and_disburse(db, _make_loan(db, companies, 150))
        db.refresh(record)
        # 冻结 200（冻结核销：持仓与冻结同减）+ 到账补缴 100（离仓）= 300 达标；
        # 借入 150 中 100 履约离仓、50 留存 → 持仓 200+150-300 = 50
        assert record.status == "compliant"
        assert float(record.cleared_amount) == pytest.approx(300, abs=0.01)
        borrower_acc = _account(db, companies["borrower"].id)
        assert float(borrower_acc.current_balance) == pytest.approx(50, abs=0.01)
        assert float(borrower_acc.frozen_balance) == pytest.approx(0, abs=0.01)

        # 到账补缴流水关联借贷单
        clear_tx = (
            db.query(AllowanceTransaction)
            .filter(AllowanceTransaction.loan_id == loan.id,
                    AllowanceTransaction.tx_type == "loan_deficit_clear")
            .one()
        )
        assert float(clear_tx.amount) == pytest.approx(100, abs=0.01)

    def test_disburse_partial_clear_leaves_deficit(self, db, companies):
        record = _setup_borrower_deficit(db, companies, 300)
        _confirm_and_disburse(db, _make_loan(db, companies, 50))
        db.refresh(record)
        assert record.status == "deficit"
        assert float(record.deficit) == pytest.approx(50, abs=0.01)

    def test_disburse_auto_clear_disabled(self, db, companies):
        record = _setup_borrower_deficit(db, companies, 300)
        loan = _make_loan(db, companies, 150, auto_clear_deficit=False)
        _confirm_and_disburse(db, loan)
        db.refresh(record)
        # 不联动：缺口仍在，借入到账全部留存自由持仓
        assert record.status == "deficit"
        borrower_acc = _account(db, companies["borrower"].id)
        assert float(borrower_acc.current_balance) == pytest.approx(350, abs=0.01)


# --------------------------------------------------------------------------- #
# 到期归还
# --------------------------------------------------------------------------- #

class TestRepay:
    def test_partial_then_full_repay(self, db, companies):
        loan = _confirm_and_disburse(db, _make_loan(db, companies, 100))
        lender_acc = _account(db, companies["lender"].id)
        borrower_acc = _account(db, companies["borrower"].id)
        assert float(lender_acc.current_balance) == 900
        assert float(borrower_acc.current_balance) == 300

        repay_loan(db, loan.id, None, amount=40)
        db.commit()
        loan = db.get(QuotaLoan, loan.id)
        assert loan.status == ACTIVE
        assert float(loan.repaid_amount) == 40
        assert float(borrower_acc.current_balance) == 260
        assert float(lender_acc.current_balance) == 940

        repay_loan(db, loan.id, None)  # 缺省全额（剩余 60）
        db.commit()
        loan = db.get(QuotaLoan, loan.id)
        assert loan.status == REPAID
        assert float(loan.repaid_amount) == 100
        assert float(borrower_acc.current_balance) == 200
        assert float(lender_acc.current_balance) == 1000

    def test_repay_cannot_exceed_outstanding(self, db, companies):
        loan = _confirm_and_disburse(db, _make_loan(db, companies, 100))
        repay_loan(db, loan.id, None, amount=150)
        db.commit()
        loan = db.get(QuotaLoan, loan.id)
        assert loan.status == REPAID
        assert float(loan.repaid_amount) == 100

    def test_repay_uses_only_free_balance(self, db, companies):
        # 借入方到账 100 后将自由配额消耗：直接原子扣减 300 吨（200 自有 + 100 借入），
        # 账户余额归零，到期无法归还，归还请求被拒绝且不产生部分扣减。
        loan = _confirm_and_disburse(db, _make_loan(db, companies, 100))
        borrower_acc = _account(db, companies["borrower"].id)
        from app.core.ledger import transactional
        with transactional(db):
            from app.core.ledger import apply_ledger_delta
            apply_ledger_delta(db, borrower_acc.id, -300, 0, 0)
        with pytest.raises(LoanError):
            repay_loan(db, loan.id, None)
        db.rollback()
        assert db.get(QuotaLoan, loan.id).status == ACTIVE

    def test_repay_idempotent(self, db, companies):
        loan = _confirm_and_disburse(db, _make_loan(db, companies, 100))
        r1 = repay_loan(db, loan.id, None, amount=50, idempotency_key="idem-repay-1")
        db.commit()
        r2 = repay_loan(db, loan.id, None, amount=50, idempotency_key="idem-repay-1")
        db.commit()
        assert r1.id == r2.id
        assert db.query(QuotaLoanRepayment).count() == 1
        assert float(db.get(QuotaLoan, loan.id).repaid_amount) == 50

    def test_repay_pre_disbursement_rejected(self, db, companies):
        loan = _make_loan(db, companies, 100)
        with pytest.raises(LoanError):
            repay_loan(db, loan.id, None)


# --------------------------------------------------------------------------- #
# 逾期 / 违约 / 追偿
# --------------------------------------------------------------------------- #

class TestOverdueAndDefault:
    def test_scan_marks_overdue(self, db, companies):
        loan = _confirm_and_disburse(db, _make_loan(
            db, companies, 100, due_date=f"{YEAR}-06-30"))
        result = mark_overdue_loans(db, None, today=f"{YEAR}-06-29")
        db.commit()
        assert result["marked"] == 0
        result = mark_overdue_loans(db, None, today=f"{YEAR}-07-01")
        db.commit()
        assert result["marked"] == 1
        assert db.get(QuotaLoan, loan.id).status == OVERDUE
        # 重复巡检幂等
        assert mark_overdue_loans(db, None, today=f"{YEAR}-07-02")["marked"] == 0
        db.commit()

    def test_default_only_from_overdue(self, db, companies):
        from app.services.loan_service import Operator

        loan = _confirm_and_disburse(db, _make_loan(
            db, companies, 100, due_date=f"{YEAR}-06-30"))
        op = Operator(id=1, username="admin", role="admin")
        with pytest.raises(LoanError):
            declare_default(db, loan.id, op, "到期未还")
        mark_overdue_loans(db, op, today=f"{YEAR}-07-01")
        db.commit()
        # 先归还 40，再宣布违约：欠额快照 = 60
        repay_loan(db, loan.id, op, amount=40)
        db.commit()
        loan = declare_default(db, loan.id, op, "到期后仍有 60 吨未清偿")
        db.commit()
        assert loan.status == DEFAULTED
        assert float(loan.defaulted_amount) == 60
        with pytest.raises(LoanError):
            declare_default(db, loan.id, op, "x")  # 原因太短

    def test_manual_recover_after_default(self, db, companies):
        from app.services.loan_service import Operator

        op = Operator(id=1, username="admin", role="admin")
        loan = _confirm_and_disburse(db, _make_loan(
            db, companies, 100, due_date=f"{YEAR}-06-30"))
        mark_overdue_loans(db, op, today=f"{YEAR}-07-01")
        db.commit()
        declare_default(db, loan.id, op, "到期未还")
        db.commit()

        # 监管按借入方汇总追偿
        result = recover_borrower_loans(db, companies["borrower"].id, YEAR, op)
        db.commit()
        assert float(result["recovered"]) == 100
        assert db.get(QuotaLoan, loan.id).status == REPAID
        lender_acc = _account(db, companies["lender"].id)
        assert float(lender_acc.current_balance) == 1000

    def test_recover_best_effort_with_insufficient_free(self, db, companies):
        from app.core.ledger import apply_ledger_delta
        from app.services.loan_service import Operator

        op = Operator(id=1, username="admin", role="admin")
        loan = _confirm_and_disburse(db, _make_loan(
            db, companies, 100, due_date=f"{YEAR}-06-30"))
        borrower_acc = _account(db, companies["borrower"].id)
        # 借入方消耗 270 吨，仅剩 30 自由可用（300 到账后持仓）
        from app.core.ledger import apply_ledger_delta
        from app.core.ledger import transactional
        with transactional(db):
            apply_ledger_delta(db, borrower_acc.id, -270, 0, 0)
        mark_overdue_loans(db, op, today=f"{YEAR}-07-01")
        db.commit()
        declare_default(db, loan.id, op, "到期未还")
        db.commit()

        result = recover_borrower_loans(db, companies["borrower"].id, YEAR, op)
        db.commit()
        assert float(result["recovered"]) == 30
        loan = db.get(QuotaLoan, loan.id)
        assert loan.status == DEFAULTED  # 未结清，仍处违约
        assert float(loan.repaid_amount) == 30
        # 补足配额后再次追偿，足额结清
        apply_ledger_delta(db, _account(db, companies["borrower"].id).id, 70, 0, 0)
        db.commit()
        result = recover_borrower_loans(db, companies["borrower"].id, YEAR, op)
        db.commit()
        assert float(result["recovered"]) == 70
        assert db.get(QuotaLoan, loan.id).status == REPAID

    def test_overdue_repay_kind_overdue(self, db, companies):
        loan = _confirm_and_disburse(db, _make_loan(
            db, companies, 100, due_date=f"{YEAR}-06-30"))
        mark_overdue_loans(db, None, today=f"{YEAR}-07-01")
        db.commit()
        repayment = repay_loan(db, loan.id, None)
        db.commit()
        assert repayment.kind == "overdue"
        assert db.get(QuotaLoan, loan.id).status == REPAID


# --------------------------------------------------------------------------- #
# 联动：订单交割到账后自动追偿
# --------------------------------------------------------------------------- #

class TestAutoRecoveryOnDelivery:
    def test_order_delivery_auto_recovers_defaulted_loan(self, db, companies):
        """借入方违约后，作为买方收到订单交割配额：先清缴（无缺口）后自动追偿。"""
        from app.services.loan_service import Operator

        op = Operator(id=1, username="admin", role="admin")
        # 借入方向出借方借 100，放款后借入方持仓 300；消耗到仅剩 30 自由可用
        loan = _confirm_and_disburse(db, _make_loan(
            db, companies, 100, due_date=f"{YEAR}-06-30"))
        from app.core.ledger import apply_ledger_delta
        apply_ledger_delta(db, _account(db, companies["borrower"].id).id, -270, 0, 0)
        db.commit()
        mark_overdue_loans(db, op, today=f"{YEAR}-07-01")
        db.commit()
        declare_default(db, loan.id, op, "到期未还")
        db.commit()
        # 手动追偿 30：剩余违约欠额 70
        recover_borrower_loans(db, companies["borrower"].id, YEAR, op)
        db.commit()
        assert float(db.get(QuotaLoan, loan.id).repaid_amount) == 30

        # 第三方企业卖给借入方（违约方）80 吨，订单交割到账后应自动追偿 70
        order = create_order(
            db,
            seller_id=companies["third"].id,
            buyer_id=companies["borrower"].id,
            year=YEAR,
            amount=80,
            price=20,
            initiator="seller",
        )
        db.commit()
        confirm_order(db, order.id, companies["borrower"].id)
        db.commit()
        deliver_order(db, order.id, companies["third"].id)
        db.commit()

        loan = db.get(QuotaLoan, loan.id)
        assert loan.status == REPAID
        assert float(loan.repaid_amount) == 100
        # 借入方：交割到账 80，自动追偿划出 70，余 10（加上此前 0 自由余额）
        borrower_acc = _account(db, companies["borrower"].id)
        assert float(borrower_acc.current_balance) == pytest.approx(10, abs=0.01)
        # 出借方收回 70：900 + 30（此前手动追偿）+ 70 = 1000
        lender_acc = _account(db, companies["lender"].id)
        assert float(lender_acc.current_balance) == pytest.approx(1000, abs=0.01)

    def test_auto_recovery_disabled_on_loan(self, db, companies):
        from app.services.loan_service import Operator

        op = Operator(id=1, username="admin", role="admin")
        loan = _confirm_and_disburse(db, _make_loan(
            db, companies, 100, due_date=f"{YEAR}-06-30",
            auto_recover_default=False))
        from app.core.ledger import apply_ledger_delta
        apply_ledger_delta(db, _account(db, companies["borrower"].id).id, -300, 0, 0)
        db.commit()
        mark_overdue_loans(db, op, today=f"{YEAR}-07-01")
        db.commit()
        declare_default(db, loan.id, op, "到期未还")
        db.commit()

        order = create_order(
            db, seller_id=companies["third"].id,
            buyer_id=companies["borrower"].id, year=YEAR, amount=100,
            initiator="seller",
        )
        db.commit()
        confirm_order(db, order.id, companies["borrower"].id)
        db.commit()
        deliver_order(db, order.id, companies["third"].id)
        db.commit()
        # 关闭自动追偿：欠额仍在，到账配额未被划走
        loan = db.get(QuotaLoan, loan.id)
        assert loan.status == DEFAULTED
        assert float(loan.repaid_amount) == 0
        borrower_acc = _account(db, companies["borrower"].id)
        assert float(borrower_acc.current_balance) == pytest.approx(100, abs=0.01)


# --------------------------------------------------------------------------- #
# 并发安全
# --------------------------------------------------------------------------- #

class TestConcurrency:
    def test_concurrent_disburse_only_once(self, db, companies):
        loan = _make_loan(db, companies, 100)
        confirm_loan(db, loan.id, companies["borrower"].id)
        db.commit()

        results = []

        def worker(cid):
            session = sessionmaker(bind=db.bind, autoflush=False)()
            try:
                disburse_loan(session, loan.id, cid)
                session.commit()
                results.append("ok")
            except LoanError:
                results.append("rejected")
            finally:
                session.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(worker, companies["lender"].id),
                pool.submit(worker, companies["borrower"].id),
            ]
            for f in futures:
                f.result()

        # 恰好一次成功放款
        assert results.count("ok") == 1
        lender_acc = _account(db, companies["lender"].id)
        borrower_acc = _account(db, companies["borrower"].id)
        assert float(lender_acc.current_balance) == 900
        assert float(borrower_acc.current_balance) == 300
        out_count = (
            db.query(AllowanceTransaction)
            .filter(AllowanceTransaction.loan_id == loan.id,
                    AllowanceTransaction.tx_type == "loan_deliver_out")
            .count()
        )
        assert out_count == 1

    def test_concurrent_partial_repays_total_capped(self, db, companies):
        loan = _confirm_and_disburse(db, _make_loan(db, companies, 100))

        def worker(amount):
            session = sessionmaker(bind=db.bind, autoflush=False)()
            try:
                repay_loan(session, loan.id, None, amount=amount)
                session.commit()
                return "ok"
            except Exception as e:
                session.rollback()
                return f"rejected:{type(e).__name__}:{e}"
            finally:
                session.close()

        # 三笔各 40，并发归还：写锁内重读未偿余额，第三笔被封顶为剩余 20 吨
        with ThreadPoolExecutor(max_workers=3) as pool:
            outcomes = list(pool.map(worker, [40, 40, 40]))

        db.expire_all()  # 并发会话已提交，主线程会话须丢弃身份映射旧快照
        loan = db.get(QuotaLoan, loan.id)
        assert float(loan.repaid_amount) == pytest.approx(100, abs=0.01)
        assert loan.status == REPAID
        assert outcomes.count("ok") == 3  # 第三笔按剩余 20 吨入账：40+40+20=100
        assert sum(
            float(r.quantity)
            for r in db.query(QuotaLoanRepayment).all()
        ) == pytest.approx(100, abs=0.01)
        # 系统总配额守恒：出借 1000 + 借入 200 + 第三方 100 = 1300
        total = (
            db.query(AllowanceAccount)
            .filter(AllowanceAccount.year == YEAR)
            .all()
        )
        assert sum(float(a.current_balance) for a in total) == pytest.approx(1300, abs=0.01)
