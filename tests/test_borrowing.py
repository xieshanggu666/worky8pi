"""配额借贷服务层测试：协商冻结、监管拨付、到期归还、违约追偿与全链路守恒。

覆盖：
- 协商状态机：建单（发起方即确认）/对方确认/监管驳回/双方撤销/监管撤销已冻结借据；
- 批准冻结：双方未确认拒绝、出借方自由可用不足拒绝、冻结占用与履约/交易互不可挤占；
- 拨付：冻结出库+借入方到账配对、联动履约缺口核销（借据归属补缴）、关闭联动；
- 归还：分次/全额/超额封顶、自由可用不足拒绝、非借入方拒绝、足额后 repaid；
- 逾期与违约：批量逾期标记幂等、仅逾期可宣告违约、尽力收回+欠额登记、
  手动追偿/订单交割到账自动追偿/竞价结算到账自动追偿、结清后 repaid；
- 报告冲正与借据归属补缴：loan_clear_refund 与 loan_deficit_clear 共用一本账，
  同一吨最多退还一次，系统总配额守恒；
- 幂等：建单/归还/追偿重复请求只生效一次；
- 并发：多线程并发拨付/归还/宣告违约，借据不超付、流水配对守恒；
- 统一账本：每笔流水同事务生成事件，全量对账 balanced。
"""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine, event, func
from sqlalchemy.orm import Session, sessionmaker

from app.core.database import Base
from app.core.event_hooks import install_ledger_hooks
from app.models import (
    AllowanceAccount,
    AllowanceTransaction,
    Company,
    ComplianceRecord,
    QuotaLoan,
    QuotaLoanRepayment,
)
from app.services.borrowing_service import (
    ACTIVE,
    CANCELLED,
    DEFAULTED,
    OVERDUE,
    PENDING,
    REJECTED,
    REPAID,
    RESERVED,
    BorrowingError,
    Operator,
    SRC_AUTO,
    SRC_MANUAL,
    approve_loan,
    cancel_loan,
    confirm_loan,
    create_loan,
    declare_default,
    disburse_loan,
    list_defaulted_loans,
    mark_overdue,
    recover_loan_defaults,
    repay_loan,
    reject_loan,
)
from app.services.quota_service import allocate_quota

YEAR = 2026
ADMIN = Operator(id=99, username="admin", role="admin")


def approx(value, rel=1e-6):
    return pytest.approx(float(value), rel=rel)


@pytest.fixture()
def db(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'borrowing.db'}",
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
def world(db):
    """出借方甲 1000 吨、借入方乙 100 吨、第三方丙 500 吨，2026 年度。"""
    companies = {}
    for code, name, quota in (("L1", "出借方甲", 1000), ("B1", "借入方乙", 100), ("C1", "第三方丙", 500)):
        c = Company(code=code, name=name, industry="电力", region="华东")
        db.add(c)
        db.flush()
        allocate_quota(db, c.id, YEAR, baseline=quota, allocation_amount=quota)
        companies[code] = c
    db.commit()
    db.expire_all()
    return companies


def _fresh(db):
    return Session(bind=db.bind)


def _account(db, company_id, year=YEAR):
    return db.query(AllowanceAccount).filter_by(company_id=company_id, year=year).one()


def _due(n: int = 7) -> str:
    return (datetime.utcnow() + timedelta(days=n)).strftime("%Y-%m-%d")


def _past_due() -> str:
    return (datetime.utcnow() - timedelta(days=1)).strftime("%Y-%m-%d")


def _make_loan(db, world, amount=100, due=None, initiator="lender", **kw):
    """出借方挂单 → 借入方确认（未批准）。"""
    due = due or _due()
    loan = create_loan(
        db,
        lender_id=world["L1"].id,
        borrower_id=world["B1"].id,
        year=YEAR,
        amount=amount,
        due_date=due,
        initiator=initiator,
        operator=ADMIN,
        **kw,
    )
    if initiator == "lender":
        # 出借方发起已确认，借入方再确认
        confirm_loan(db, loan.id, world["B1"].id)
    else:
        confirm_loan(db, loan.id, world["L1"].id)
    db.expire_all()
    return db.get(QuotaLoan, loan.id)


def _active_loan(db, world, amount=100, due=None, **kw):
    """协商→确认→批准冻结→拨付，返回 active 借据。"""
    loan = _make_loan(db, world, amount=amount, due=due, **kw)
    approve_loan(db, loan.id, ADMIN)
    disburse_loan(db, loan.id, ADMIN)
    db.expire_all()
    return db.get(QuotaLoan, loan.id)


def _tx_sum(db, account_id, types):
    return float(
        db.query(func.coalesce(func.sum(AllowanceTransaction.amount), 0))
        .filter(AllowanceTransaction.account_id == account_id,
                AllowanceTransaction.tx_type.in_(types))
        .scalar() or 0
    )


# --------------------------------------------------------------------------- #
# 协商 / 批准 / 驳回 / 撤销
# --------------------------------------------------------------------------- #

def test_create_loan_validations(db, world):
    with pytest.raises(BorrowingError):
        create_loan(db, lender_id=world["L1"].id, borrower_id=world["B1"].id,
                    year=YEAR, amount=0, due_date=_due())
    with pytest.raises(BorrowingError):
        create_loan(db, lender_id=world["L1"].id, borrower_id=world["B1"].id,
                    year=YEAR, amount=10, due_date="")
    with pytest.raises(BorrowingError):
        create_loan(db, lender_id=world["L1"].id, borrower_id=world["L1"].id,
                    year=YEAR, amount=10, due_date=_due())
    # 缺账户年度拒绝
    with pytest.raises(BorrowingError):
        create_loan(db, lender_id=world["L1"].id, borrower_id=world["B1"].id,
                    year=2030, amount=10, due_date=_due())


def test_create_loan_initiator_confirmed_and_idempotent(db, world):
    loan = create_loan(
        db, lender_id=world["L1"].id, borrower_id=world["B1"].id,
        year=YEAR, amount=100, due_date=_due(), initiator="lender",
        idempotency_key="k1",
    )
    assert loan.lender_confirmed == 1 and loan.borrower_confirmed == 0
    again = create_loan(
        db, lender_id=world["L1"].id, borrower_id=world["B1"].id,
        year=YEAR, amount=100, due_date=_due(), initiator="lender",
        idempotency_key="k1",
    )
    assert again.id == loan.id


def test_confirm_requires_party_and_both(db, world):
    loan = create_loan(
        db, lender_id=world["L1"].id, borrower_id=world["B1"].id,
        year=YEAR, amount=100, due_date=_due(),
    )
    with pytest.raises(BorrowingError):
        confirm_loan(db, loan.id, world["C1"].id)
    confirm_loan(db, loan.id, world["B1"].id)
    db.expire_all()
    loan = db.get(QuotaLoan, loan.id)
    assert loan.lender_confirmed == 1 and loan.borrower_confirmed == 1
    assert loan.confirmed_at is not None
    # 重复确认幂等
    confirm_loan(db, loan.id, world["B1"].id)


def test_approve_requires_both_confirmed(db, world):
    loan = create_loan(
        db, lender_id=world["L1"].id, borrower_id=world["B1"].id,
        year=YEAR, amount=100, due_date=_due(),
    )
    with pytest.raises(BorrowingError):
        approve_loan(db, loan.id, ADMIN)


def test_approve_freezes_lender_reserved(db, world):
    loan = _make_loan(db, world, amount=300)
    lender = _account(db, world["L1"].id)
    assert float(lender.reserved_balance) == 0
    approve_loan(db, loan.id, ADMIN)
    db.expire_all()
    lender = _account(db, world["L1"].id)
    assert float(lender.current_balance) == 1000
    assert float(lender.reserved_balance) == 300
    assert _tx_sum(db, lender.id, ["loan_reserve"]) == 300
    loan = db.get(QuotaLoan, loan.id)
    assert loan.status == RESERVED
    # 重复批准幂等
    approve_loan(db, loan.id, ADMIN)


def test_approve_rejected_when_lender_free_insufficient(db, world):
    # 出借方再挂一张 800 吨借据并冻结，自由可用只剩 200
    first = _active_loan(db, world, amount=800)
    assert first.status == ACTIVE  # 已拨付，出库 800
    second = _make_loan(db, world, amount=500)
    with pytest.raises(BorrowingError):
        approve_loan(db, second.id, ADMIN)
    # 批准失败不改变状态、不留冻结
    db.expire_all()
    lender = _account(db, world["L1"].id)
    assert float(lender.current_balance) == 200
    second = db.get(QuotaLoan, second.id)
    assert second.status == PENDING


def test_reject_pending_loan(db, world):
    loan = _make_loan(db, world, amount=50)
    reject_loan(db, loan.id, ADMIN, "材料不齐")
    db.expire_all()
    loan = db.get(QuotaLoan, loan.id)
    assert loan.status == REJECTED and loan.reject_reason == "材料不齐"
    with pytest.raises(BorrowingError):
        approve_loan(db, loan.id, ADMIN)


def test_party_cancel_pending_has_no_ledger_side_effect(db, world):
    loan = _make_loan(db, world, amount=50)
    cancel_loan(db, loan.id, company_id=world["B1"].id, operator=ADMIN, reason="协商取消")
    db.expire_all()
    loan = db.get(QuotaLoan, loan.id)
    assert loan.status == CANCELLED
    lender = _account(db, world["L1"].id)
    assert float(lender.reserved_balance) == 0
    # 非参与方不能撤销
    another = _make_loan(db, world, amount=10)
    with pytest.raises(BorrowingError):
        cancel_loan(db, another.id, company_id=world["C1"].id)


def test_regulator_cancel_reserved_releases_freeze(db, world):
    loan = _make_loan(db, world, amount=200)
    approve_loan(db, loan.id, ADMIN)
    lender = _account(db, world["L1"].id)
    assert float(lender.reserved_balance) == 200
    # 企业不能自行撤销已冻结借据
    with pytest.raises(BorrowingError):
        cancel_loan(db, loan.id, company_id=world["L1"].id)
    cancel_loan(db, loan.id, company_id=None, operator=ADMIN, reason="监管叫停")
    db.expire_all()
    lender = _account(db, world["L1"].id)
    assert float(lender.reserved_balance) == 0
    assert _tx_sum(db, lender.id, ["loan_release"]) == 200
    loan = db.get(QuotaLoan, loan.id)
    assert loan.status == CANCELLED and loan.cancelled_by == 0


# --------------------------------------------------------------------------- #
# 拨付 / 履约联动
# --------------------------------------------------------------------------- #

def test_disburse_transfers_and_pair_conservation(db, world):
    loan = _make_loan(db, world, amount=400)
    with pytest.raises(BorrowingError):
        disburse_loan(db, loan.id, ADMIN)  # 未批准
    approve_loan(db, loan.id, ADMIN)
    disburse_loan(db, loan.id, ADMIN)
    db.expire_all()
    lender = _account(db, world["L1"].id)
    borrower = _account(db, world["B1"].id)
    assert float(lender.current_balance) == 600 and float(lender.reserved_balance) == 0
    assert float(borrower.current_balance) == 500
    assert _tx_sum(db, lender.id, ["loan_disburse_out"]) == 400
    assert _tx_sum(db, borrower.id, ["loan_disburse_in"]) == 400
    loan = db.get(QuotaLoan, loan.id)
    assert loan.status == ACTIVE and loan.disbursed_at is not None
    # 重复拨付幂等
    disburse_loan(db, loan.id, ADMIN)


def _make_borrower_deficit(db, company, emission=300):
    """构造借入方履约缺口：直接建活跃履约记录并冻结其全部自有配额。"""
    from app.models import ActivityData, CalculationMethod, EmissionFactor, EmissionScope
    from app.services.calculation_service import recalc_company_year
    from app.services.mrv_service import approve_report, generate_report, submit_report

    cid = company.id
    db.add(EmissionScope(company_id=cid, scope="2", category="外购电力", name="厂区用电"))
    db.add(CalculationMethod(
        method_code="ELEC", name="外购电力排放因子法", scope="2",
        formula_type="activity_factor"))
    factor = 0.5
    db.add(EmissionFactor(
        factor_code="ELEC-GRID", name="外购电力", scope="2", unit="tCO2/MWh",
        value=factor, source="电网因子", valid_from="2025-01-01", valid_to="2027-12-31"))
    db.flush()
    sid = db.query(EmissionScope).filter_by(company_id=cid, scope="2").one().id
    db.add(ActivityData(
        company_id=cid, scope_id=sid, year=YEAR, period="monthly",
        activity_type="外购电力", unit="MWh", quantity=emission / factor,
        data_source="台账", verified=1))
    db.commit()
    recalc_company_year(db, cid, YEAR)
    report = generate_report(db, cid, YEAR)
    submit_report(db, report)
    approve_report(db, report, verifier_id=1)
    db.expire_all()


def test_disburse_auto_clears_borrower_deficit_with_loan_attribution(db, world):
    # 借入方 100 吨配额；排放 300 → 冻结 100，缺口 200
    _make_borrower_deficit(db, world["B1"], emission=300)
    loan = _active_loan(db, world, amount=200)
    db.expire_all()
    borrower = _account(db, world["B1"].id)
    # 借入方到账 200 随即全部补缴缺口：持仓 = 100(自有冻结) + 200(到账) - 100(冻结核销)
    # - 200(到账补缴) = 0
    assert float(borrower.current_balance) == approx(0)
    assert float(borrower.frozen_balance) == approx(0)
    record = db.query(ComplianceRecord).filter_by(
        company_id=world["B1"].id, year=YEAR, is_active=1).one()
    assert record.status == "compliant"
    assert float(record.cleared_amount) == approx(300)
    # 借据归属补缴流水 200
    clear = db.query(AllowanceTransaction).filter_by(
        account_id=borrower.id, tx_type="loan_deficit_clear", quota_loan_id=loan.id).all()
    assert sum(float(t.amount) for t in clear) == approx(200)


def test_disburse_auto_clear_disabled_keeps_deficit(db, world):
    _make_borrower_deficit(db, world["B1"], emission=300)
    loan = _active_loan(db, world, amount=200, auto_clear_deficit=False)
    db.expire_all()
    borrower = _account(db, world["B1"].id)
    # 到账 200 未联动清缴：自由可用 200 + 自有冻结 100，持仓 300
    assert float(borrower.current_balance) == approx(300)
    assert float(borrower.frozen_balance) == approx(100)
    record = db.query(ComplianceRecord).filter_by(
        company_id=world["B1"].id, year=YEAR, is_active=1).one()
    assert record.status == "deficit"
    assert _tx_sum(db, borrower.id, ["loan_deficit_clear"]) == 0


# --------------------------------------------------------------------------- #
# 归还
# --------------------------------------------------------------------------- #

def test_repay_full_and_partial_cap_and_close(db, world):
    loan = _active_loan(db, world, amount=300)
    lender0 = float(_account(db, world["L1"].id).current_balance)  # 700
    borrower0 = float(_account(db, world["B1"].id).current_balance)  # 400
    # 非借入方不能归还
    with pytest.raises(BorrowingError):
        repay_loan(db, loan.id, world["L1"].id)
    r1 = repay_loan(db, loan.id, world["B1"].id, amount=100, operator=ADMIN,
                    idempotency_key="rp1")
    assert float(r1.quantity) == 100
    # 幂等：同键重复只一笔
    r1b = repay_loan(db, loan.id, world["B1"].id, amount=100, operator=ADMIN,
                     idempotency_key="rp1")
    assert r1b.id == r1.id
    db.expire_all()
    loan = db.get(QuotaLoan, loan.id)
    assert float(loan.returned_amount) == 100 and loan.status == ACTIVE
    # 超额请求自动封顶到剩余 200
    repay_loan(db, loan.id, world["B1"].id, amount=999)
    db.expire_all()
    loan = db.get(QuotaLoan, loan.id)
    assert float(loan.returned_amount) == 300 and loan.status == REPAID
    assert loan.closed_at is not None
    lender = _account(db, world["L1"].id)
    borrower = _account(db, world["B1"].id)
    assert float(lender.current_balance) == approx(lender0 + 300)
    assert float(borrower.current_balance) == approx(borrower0 - 300)
    assert _tx_sum(db, lender.id, ["loan_repay_in"]) == 300
    assert _tx_sum(db, borrower.id, ["loan_repay_out"]) == 300


def test_repay_uses_only_free_balance(db, world):
    # 借入方到账 300 后自由可用为 400；再把 350 转为交易占用，仅剩 50
    from app.core.ledger import (
        apply_ledger_delta, lock_row_for_write, transactional,
    )
    loan = _active_loan(db, world, amount=300)
    borrower = _account(db, world["B1"].id)
    with transactional(db):
        lock_row_for_write(db, borrower.id)
        apply_ledger_delta(db, borrower.id, 0, 0, 350)
    # 请求归还 100 但自由可用仅剩 50：尽力归还 50（绝不触碰 350 占用）
    repayment = repay_loan(db, loan.id, world["B1"].id, amount=100)
    assert float(repayment.quantity) == approx(50)
    db.expire_all()
    borrower = _account(db, world["B1"].id)
    assert float(borrower.reserved_balance) == 350
    loan = db.get(QuotaLoan, loan.id)
    assert float(loan.returned_amount) == approx(50)


class _noop_ctx:
    def __init__(self, db):
        self.db = db

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_overdue_marking_batch_idempotent(db, world):
    active_future = _active_loan(db, world, amount=10, due=_due(30))
    active_past = _active_loan(db, world, amount=20, due=_past_due())
    marked = mark_overdue(db, YEAR, operator=ADMIN)
    assert {l.id for l in marked} == {active_past.id}
    # 重复执行不再标记
    assert mark_overdue(db, YEAR, operator=ADMIN) == []
    db.expire_all()
    assert db.get(QuotaLoan, active_past.id).status == OVERDUE
    assert db.get(QuotaLoan, active_future.id).status == ACTIVE


# --------------------------------------------------------------------------- #
# 违约宣告与追偿
# --------------------------------------------------------------------------- #

def _defaulted_loan(db, world, amount=300, free_left=50):
    """拨付 amount；再把借入方自由可用压到 free_left，逾期并宣告违约。

    返回 (loan, clawback, defaulted_amount)。
    """
    from app.core.ledger import apply_ledger_delta, lock_row_for_write, transactional
    loan = _active_loan(db, world, amount=amount, due=_past_due())
    borrower = _account(db, world["B1"].id)
    # 借入方当前 100 + amount；把多余部分以交易占用锁住，只留 free_left 自由可用
    want_reserve = round((100 + amount) - free_left, 4)
    with transactional(db):
        lock_row_for_write(db, borrower.id)
        apply_ledger_delta(db, borrower.id, 0, 0, want_reserve)
    mark_overdue(db, YEAR, operator=ADMIN)
    result = declare_default(db, loan.id, ADMIN, "到期未清偿")
    db.expire_all()
    loan = db.get(QuotaLoan, loan.id)
    return loan, result


def test_declare_default_only_for_overdue(db, world):
    loan = _active_loan(db, world, amount=50)  # due 在未来
    with pytest.raises(BorrowingError):
        declare_default(db, loan.id, ADMIN, "未逾期不能违约")


def test_declare_default_partial_clawback_registers_arrears(db, world):
    loan, result = _defaulted_loan(db, world, amount=300, free_left=50)
    assert result["recovered"] == approx(50)
    assert result["defaulted"] == approx(250)
    assert loan.status == DEFAULTED
    assert float(loan.defaulted_amount) == approx(250)
    assert float(loan.returned_amount) == approx(50)
    borrower = _account(db, world["B1"].id)
    lender = _account(db, world["L1"].id)
    assert _tx_sum(db, borrower.id, ["loan_default_clawback_out"]) == 50
    assert _tx_sum(db, lender.id, ["loan_default_clawback_in"]) == 50
    assert list_defaulted_loans(db, borrower_id=world["B1"].id, year=YEAR)


def test_declare_default_full_recovery_closes_loan(db, world):
    # 借入方自由可用足以覆盖全部未偿本金 → 直接 repaid，不留欠额
    loan = _active_loan(db, world, amount=300, due=_past_due())
    mark_overdue(db, YEAR, operator=ADMIN)
    result = declare_default(db, loan.id, ADMIN, "逾期但足额收回")
    assert result["recovered"] == approx(300) and result["defaulted"] == approx(0)
    db.expire_all()
    assert db.get(QuotaLoan, loan.id).status == REPAID


def test_manual_recovery_best_effort_then_close(db, world):
    loan, _ = _defaulted_loan(db, world, amount=300, free_left=0)
    from app.core.ledger import apply_ledger_delta, transactional
    # 无可用时手动追偿报错
    with pytest.raises(BorrowingError):
        recover_loan_defaults(db, world["B1"].id, YEAR, ADMIN)
    # 借入方从外部买入 120 吨
    borrower = _account(db, world["B1"].id)
    with transactional(db):
        apply_ledger_delta(db, borrower.id, 120, 0, 0)
    result = recover_loan_defaults(db, world["B1"].id, YEAR, ADMIN)
    assert float(result["recovered"]) == approx(120)
    db.expire_all()
    loan = db.get(QuotaLoan, loan.id)
    assert float(loan.recovered_default) == 120 and loan.status == DEFAULTED
    borrower = _account(db, world["B1"].id)
    lender = _account(db, world["L1"].id)
    assert _tx_sum(db, borrower.id, ["loan_default_recover_out"]) == 120
    assert _tx_sum(db, lender.id, ["loan_default_recover_in"]) == 120
    # 再补足剩余 130 欠额（多充 50 也只追偿欠额），追偿结清
    with transactional(db):
        apply_ledger_delta(db, borrower.id, 180, 0, 0)
    recover_loan_defaults(db, world["B1"].id, YEAR, ADMIN)
    db.expire_all()
    loan = db.get(QuotaLoan, loan.id)
    assert loan.status == REPAID
    # 违约宣告时 free_left=0 未收回，两次外部补足后追偿 120 + 180 = 300 全部结清
    assert float(loan.recovered_default) == approx(300)
    assert float(loan.returned_amount) == approx(300)
    # 追偿明细来源正确（本例宣告时 free_left=0 无 clawback，仅追偿明细）
    sources = {r.source for r in db.query(QuotaLoanRepayment).filter_by(loan_id=loan.id)}
    assert sources == {SRC_MANUAL}


def test_auto_recovery_on_trade_delivery(db, world):
    """借入方作为买方在订单交割到账后，同事务自动追偿借据违约欠额。"""
    from app.services.trade_order_service import create_order, confirm_order, deliver_order

    loan, _ = _defaulted_loan(db, world, amount=300, free_left=0)
    # 第三方丙卖 200 给违约借入方乙：丙账户 500，乙买入
    order = create_order(
        db, seller_id=world["C1"].id, buyer_id=world["B1"].id,
        year=YEAR, amount=200, price=80, initiator="seller",
    )
    confirm_order(db, order.id, world["B1"].id)
    deliver_order(db, order.id, world["C1"].id)
    db.expire_all()
    loan = db.get(QuotaLoan, loan.id)
    # 到账 200 全部自动追偿给出借方甲（违约宣告时收回 0 + 本次 200）
    assert float(loan.recovered_default) == approx(200)
    assert float(loan.returned_amount) == approx(200)
    assert loan.status == DEFAULTED  # 仍欠 100
    lender = _account(db, world["L1"].id)
    third = _account(db, world["C1"].id)
    assert _tx_sum(db, lender.id, ["loan_default_recover_in"]) == 200
    # 丙划出 200，乙收到后立即偿出：乙持仓不应沉淀这 200（仍为 400 全占用）
    borrower = _account(db, world["B1"].id)
    assert float(third.current_balance) == approx(300)
    assert float(borrower.current_balance) == approx(400)
    assert float(borrower.reserved_balance) == approx(400)
    repay_docs = db.query(QuotaLoanRepayment).filter_by(loan_id=loan.id).all()
    assert any(r.source == SRC_AUTO for r in repay_docs)


def test_auto_recovery_respects_loan_switch_off(db, world):
    """借据关闭 auto_recover_default 时，订单交割到账不自动追偿，手动追偿仍可用。"""
    from app.core.ledger import (
        apply_ledger_delta, lock_row_for_write, transactional,
    )
    from app.services.trade_order_service import (
        confirm_order, create_order, deliver_order,
    )

    loan = _active_loan(db, world, amount=300, due=_past_due(),
                        auto_recover_default=False)
    borrower = _account(db, world["B1"].id)
    with transactional(db):
        lock_row_for_write(db, borrower.id)
        apply_ledger_delta(db, borrower.id, 0, 0, 400)
    mark_overdue(db, YEAR, operator=ADMIN)
    declare_default(db, loan.id, ADMIN, "到期未清偿")
    db.expire_all()

    # 第三方卖 200 给违约借入方：到账不应被自动追偿
    order = create_order(
        db, seller_id=world["C1"].id, buyer_id=world["B1"].id,
        year=YEAR, amount=200, price=10,
    )
    confirm_order(db, order.id, world["B1"].id)
    deliver_order(db, order.id, world["C1"].id)
    db.expire_all()
    loan = db.get(QuotaLoan, loan.id)
    assert loan.status == DEFAULTED
    assert float(loan.recovered_default) == 0
    borrower = _account(db, world["B1"].id)
    assert float(borrower.current_balance) == 600  # 400 占用 + 200 到账未划走

    # 监管手动追偿不受开关限制：自由可用 200 全部收回
    result = recover_loan_defaults(db, world["B1"].id, YEAR, ADMIN)
    assert float(result["recovered"]) == 200
    db.expire_all()
    loan = db.get(QuotaLoan, loan.id)
    assert float(loan.recovered_default) == 200 and loan.status == DEFAULTED


def test_auto_recovery_on_auction_settle(db, world):
    """违约借入方在后续竞价场次作为买方结算到账后自动追偿。"""
    from app.services.auction_service import (
        create_session, open_session, place_bid, run_matching, settle_session,
    )

    loan, _ = _defaulted_loan(db, world, amount=300, free_left=0)
    session = create_session(db, year=YEAR, name="追偿场次", operator=ADMIN)
    open_session(db, session.id, ADMIN)
    # 丙卖 150、乙（违约借入方）买 150
    place_bid(db, session.id, world["C1"].id, "sell", 150, 10, operator=ADMIN)
    place_bid(db, session.id, world["B1"].id, "buy", 150, 10, operator=ADMIN)
    run_matching(db, session.id, ADMIN)
    settle_session(db, session.id, ADMIN)
    db.expire_all()
    loan = db.get(QuotaLoan, loan.id)
    assert float(loan.recovered_default) == approx(150)
    assert float(loan.returned_amount) == approx(150)
    lender = _account(db, world["L1"].id)
    assert _tx_sum(db, lender.id, ["loan_default_recover_in"]) == 150


# --------------------------------------------------------------------------- #
# 报告冲正 × 借据归属补缴（同一吨最多退还一次）+ 守恒
# --------------------------------------------------------------------------- #

def test_report_reversal_refunds_loan_attributed_clearance(db, world):
    from app.services.mrv_service import (
        approve_report, generate_report, submit_report,
    )
    from app.services.quota_service import reverse_approved_report

    _make_borrower_deficit(db, world["B1"], emission=300)
    loan = _active_loan(db, world, amount=200)
    db.expire_all()
    borrower = _account(db, world["B1"].id)
    # 已达标：cleared 300（frozen_clear 100 + loan_deficit_clear 200）
    report = None
    from app.models.report import MrvReport
    report = db.query(MrvReport).filter_by(
        company_id=world["B1"].id, year=YEAR).order_by(MrvReport.id.desc()).first()
    reverse_approved_report(db, report, operator_id=1, reason="排放数据修正")
    db.expire_all()
    # 借据归属的 200 以 loan_clear_refund 退还（非 paired，单边入账）
    refund = _tx_sum(db, borrower.id, ["loan_clear_refund"])
    assert refund == approx(200)
    # 借入方履约 300 全部回退：冻结 100 解冻 + 借据补缴 200 退还，持仓 = 100 + 200 = 300
    assert float(borrower.current_balance) == approx(300)
    assert float(borrower.frozen_balance) == approx(0)
    # 借据仍是 active（报告冲正不影响债权债务关系）
    assert db.get(QuotaLoan, loan.id).status == ACTIVE


def test_loan_clearance_ledger_conservation_and_reconcile(db, world):
    """完整链路后统一事件链对账 balanced，系统总配额守恒。"""
    from app.services.trading_service import transfer as transfer_allowance

    _make_borrower_deficit(db, world["B1"], emission=300)
    loan = _active_loan(db, world, amount=200)
    db.expire_all()
    # 借入方到账配额已全部用于清缴，需从外部市场买入 200 才能归还
    borrower = _account(db, world["B1"].id)
    transfer_allowance(db, borrower, 200, "buy", counterparty="碳市场")
    # 分次归还 200
    repay_loan(db, loan.id, world["B1"].id, amount=120)
    repay_loan(db, loan.id, world["B1"].id, amount=80)
    db.expire_all()

    from app.services.reconciliation_service import run_reconciliation
    run = run_reconciliation(db)
    assert run.status == "balanced", run.discrepancies_json
    # 年度系统总持仓：初始 1600 + 外部买入 200 − 清缴离仓 300 = 1500
    total = float(
        db.query(func.coalesce(func.sum(AllowanceAccount.current_balance), 0))
        .filter(AllowanceAccount.year == YEAR).scalar())
    assert total == approx(1500)


# --------------------------------------------------------------------------- #
# 并发
# --------------------------------------------------------------------------- #

def test_concurrent_disburse_only_once(db, world):
    loan = _make_loan(db, world, amount=200)
    approve_loan(db, loan.id, ADMIN)
    db.commit()

    def do():
        s = _fresh(db)
        try:
            disburse_loan(s, loan.id, ADMIN)
        except BorrowingError:
            pass
        finally:
            s.close()

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda _: do(), range(8)))

    s = _fresh(db)
    lender = s.query(AllowanceAccount).filter_by(
        company_id=world["L1"].id, year=YEAR).one()
    borrower = s.query(AllowanceAccount).filter_by(
        company_id=world["B1"].id, year=YEAR).one()
    assert float(lender.current_balance) == 800
    assert float(borrower.current_balance) == 300
    assert s.query(AllowanceTransaction).filter_by(
        account_id=lender.id, tx_type="loan_disburse_out").count() == 1
    assert s.query(QuotaLoan).filter_by(id=loan.id).one().status == ACTIVE
    s.close()


def test_concurrent_repay_never_exceeds_principal(db, world):
    loan = _active_loan(db, world, amount=200)
    db.commit()

    def do(_):
        s = _fresh(db)
        try:
            repay_loan(s, loan.id, world["B1"].id, amount=60)
        except BorrowingError:
            pass
        finally:
            s.close()

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(do, range(8)))

    s = _fresh(db)
    loan_obj = s.query(QuotaLoan).filter_by(id=loan.id).one()
    assert float(loan_obj.returned_amount) == approx(200)
    assert loan_obj.status == REPAID
    borrower = s.query(AllowanceAccount).filter_by(
        company_id=world["B1"].id, year=YEAR).one()
    lender = s.query(AllowanceAccount).filter_by(
        company_id=world["L1"].id, year=YEAR).one()
    out_sum = s.query(func.coalesce(func.sum(AllowanceTransaction.amount), 0)).filter(
        AllowanceTransaction.account_id == borrower.id,
        AllowanceTransaction.tx_type == "loan_repay_out").scalar()
    in_sum = s.query(func.coalesce(func.sum(AllowanceTransaction.amount), 0)).filter(
        AllowanceTransaction.account_id == lender.id,
        AllowanceTransaction.tx_type == "loan_repay_in").scalar()
    assert float(out_sum) == approx(200)
    assert float(in_sum) == approx(200)
    s.close()
