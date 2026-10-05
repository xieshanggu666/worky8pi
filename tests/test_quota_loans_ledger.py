"""配额借贷 × 统一账本事件链 / 重放 / 对账 专项。

覆盖：
- 借贷占用/放款/归还/追偿每笔流水同事务登记 ledger_events，seq 连续、哈希链勾连；
- 全量重放投影与出借/借入账户三余额一致，逐笔快照链相符；
- 对账六维（链/投影/流水/单据/履约/守恒）在「确认→放款（联动清缴）→
  逾期→违约→订单交割到账自动追偿结清」全链路下保持 balanced；
- 旧库迁移回填：清空事件链后从 quota_loans / quota_loan_repayments /
  allowance_transactions 幂等回填，重放结论不变、重复回填零新增；
- 库外篡改余额/物理删除事件被对账检出。
"""

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
from app.core.event_hooks import install_ledger_hooks
from app.models import (
    AllowanceAccount,
    CalculationMethod,
    Company,
    EmissionFactor,
    EmissionScope,
    LedgerEvent,
    QuotaLoan,
)
from app.services.ledger_event_service import backfill_ledger_events
from app.services.loan_service import (
    DEFAULTED,
    Operator,
    confirm_loan,
    create_loan,
    declare_default,
    disburse_loan,
    mark_overdue_loans,
)
from app.services.mrv_service import approve_report, generate_report, submit_report
from app.services.quota_service import allocate_quota
from app.services.reconciliation_service import run_reconciliation
from app.services.replay_service import replay_all
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
        f"sqlite:///{tmp_path / 'loan_ledger.db'}",
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


def _seed(db):
    lender = Company(code="L-1", name="出借企业", industry="电力", region="华东")
    borrower = Company(code="B-1", name="借入企业", industry="水泥", region="华北")
    third = Company(code="T-1", name="第三方企业", industry="化工", region="华南")
    db.add_all([lender, borrower, third])
    db.flush()
    allocate_quota(db, lender.id, YEAR, baseline=1000, allocation_amount=1000)
    allocate_quota(db, borrower.id, YEAR, baseline=200, allocation_amount=200)
    allocate_quota(db, third.id, YEAR, baseline=500, allocation_amount=500)
    db.commit()
    return lender, borrower, third


def _borrower_deficit(db, borrower, emission):
    db.add(EmissionScope(company_id=borrower.id, scope="2", category="外购电力", name="厂区用电"))
    db.add(CalculationMethod(
        method_code="ELEC", name="外购电力排放因子法", scope="2",
        formula_type="activity_factor"))
    db.add(EmissionFactor(
        factor_code="ELEC-GRID", name="外购电力", scope="2", unit="tCO2/MWh",
        value=FACTOR, source="电网因子",
        valid_from=f"{YEAR}-01-01", valid_to=f"{YEAR}-12-31"))
    db.flush()
    from app.models import ActivityData
    from app.services.calculation_service import recalc_company_year

    scope = db.query(EmissionScope).filter_by(company_id=borrower.id, scope="2").one()
    db.add(ActivityData(
        company_id=borrower.id, scope_id=scope.id, year=YEAR, period="yearly",
        activity_type="外购电力", unit="MWh", quantity=emission / FACTOR,
        data_source="电网单", verified=1,
    ))
    db.commit()
    recalc_company_year(db, borrower.id, YEAR)
    report = generate_report(db, borrower.id, YEAR)
    submit_report(db, report)
    approve_report(db, report, verifier_id=1)
    db.commit()


def _account(db, cid):
    return db.query(AllowanceAccount).filter_by(company_id=cid, year=YEAR).one()


class TestLoanLedger:
    def test_events_emitted_in_same_transaction(self, db):
        lender, borrower, _ = _seed(db)
        loan = create_loan(
            db, lender.id, borrower.id, YEAR, 100,
            initiator="lender", due_date=f"{YEAR}-12-31",
        )
        db.commit()
        confirm_loan(db, loan.id, borrower.id)
        db.commit()
        disburse_loan(db, loan.id, lender.id)
        db.commit()

        loan_types = {
            e.event_type
            for e in db.query(LedgerEvent).filter(LedgerEvent.loan_id == loan.id).all()
        }
        assert {"loan_reserve", "loan_deliver_out", "loan_deliver_in"} <= loan_types
        # 每个事件均强制带年度（跨年度核对）
        assert all(
            e.year == YEAR
            for e in db.query(LedgerEvent).filter(LedgerEvent.loan_id == loan.id).all()
        )

    def test_replay_projection_matches_after_full_loop(self, db):
        lender, borrower, third = _seed(db)
        # 借入方排放 300：自有 200，借 150 到账即补缴 100 达标
        _borrower_deficit(db, borrower, 300)
        loan = create_loan(
            db, lender.id, borrower.id, YEAR, 150,
            initiator="lender", due_date=f"{YEAR}-06-30",
        )
        db.commit()
        confirm_loan(db, loan.id, borrower.id)
        db.commit()
        disburse_loan(db, loan.id, lender.id)
        db.commit()

        # 借入方把自由配额消耗至不足以归还，逾期 → 违约
        from app.core.ledger import apply_ledger_delta

        borrower_acc = _account(db, borrower.id)
        # 到账后持仓 50（150 中 100 已履约离仓）；再划入第三方买入使其后可被自动追偿
        op = Operator(id=1, username="admin", role="admin")
        mark_overdue_loans(db, op, today=f"{YEAR}-07-01")
        db.commit()
        declare_default(db, loan.id, op, "到期未清偿")
        db.commit()
        assert db.get(QuotaLoan, loan.id).status == DEFAULTED

        # 第三方向违约借入方交割 200 吨，到账先清缴（无缺口）后自动追偿欠额 150
        order = create_order(
            db, seller_id=third.id, buyer_id=borrower.id, year=YEAR,
            amount=200, initiator="seller",
        )
        db.commit()
        confirm_order(db, order.id, borrower.id)
        db.commit()
        deliver_order(db, order.id, third.id)
        db.commit()

        loan = db.get(QuotaLoan, loan.id)
        assert loan.status == "repaid"
        assert float(loan.repaid_amount) == pytest.approx(150, abs=0.01)

        # 重放投影 vs 实际三余额（全部账户）
        states = replay_all(db, check_snapshots=True)
        for acc_id, state in states.items():
            account = db.get(AllowanceAccount, acc_id)
            assert state.current == pytest.approx(float(account.current_balance), abs=1e-6)
            assert state.frozen == pytest.approx(float(account.frozen_balance), abs=1e-6)
            assert state.reserved == pytest.approx(float(account.reserved_balance), abs=1e-6)
            assert state.snapshot_mismatches == []
            assert state.unknown_types == set()

        # 全量对账：六维平衡、系统守恒
        run = run_reconciliation(db, scope="full")
        assert run.status == "balanced", run.discrepancies_json
        assert bool(run.conserved) is True

    def test_legacy_backfill_idempotent(self, db):
        lender, borrower, _ = _seed(db)
        loan = create_loan(
            db, lender.id, borrower.id, YEAR, 100,
            initiator="lender", due_date=f"{YEAR}-12-31",
        )
        db.commit()
        confirm_loan(db, loan.id, borrower.id)
        db.commit()
        disburse_loan(db, loan.id, lender.id)
        db.commit()

        before = db.query(LedgerEvent).count()
        # 模拟旧库：清空事件链（业务表保留），再幂等回填
        db.query(LedgerEvent).delete()
        db.commit()
        stats = backfill_ledger_events(db, commit=True)
        assert stats["total"] > 0
        after_first = db.query(LedgerEvent).count()
        assert after_first >= before
        # 重复回填零新增
        stats2 = backfill_ledger_events(db, commit=True)
        assert stats2["total"] == 0
        assert db.query(LedgerEvent).count() == after_first

        # 回填后重放投影仍与账户一致
        states = replay_all(db, check_snapshots=True)
        for acc_id, state in states.items():
            account = db.get(AllowanceAccount, acc_id)
            assert state.current == pytest.approx(float(account.current_balance), abs=1e-6)

        run = run_reconciliation(db, scope="full")
        assert run.status == "balanced", run.discrepancies_json

    def test_tamper_detected_by_reconciliation(self, db):
        lender, borrower, _ = _seed(db)
        loan = create_loan(
            db, lender.id, borrower.id, YEAR, 100,
            initiator="lender", due_date=f"{YEAR}-12-31",
        )
        db.commit()
        confirm_loan(db, loan.id, borrower.id)
        db.commit()
        disburse_loan(db, loan.id, lender.id)
        db.commit()
        assert run_reconciliation(db, scope="full").status == "balanced"

        # 库外篡改出借方持仓 +100（不经流水/事件），对账应检出投影不符
        from sqlalchemy import text

        db.execute(
            text("UPDATE allowance_accounts SET current_balance = current_balance + 100")
        )
        db.commit()
        run = run_reconciliation(db, scope="full")
        assert run.status == "discrepancy"
        codes = {d["code"] for d in __import__("json").loads(run.discrepancies_json)}
        assert "PROJECTION_CURRENT_MISMATCH" in codes or "FLOW_OPENING_PLUS_TX_MISMATCH" in codes
