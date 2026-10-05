"""配额借贷 API 集成测试：角色边界、三方协同状态机、HTTP 幂等与越权审计。"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.database import Base, get_db
from app.core.security import hash_password
from app.main import app
from app.models import AllowanceAccount, Company, QuotaLoan, User
from app.services.quota_service import allocate_quota

YEAR = 2026


def _seed_db(TestingSession):
    db = TestingSession()
    pwd, salt = hash_password("123456")
    c1 = Company(code="L-001", name="出借企业", industry="电力", region="华东")
    c2 = Company(code="B-001", name="借入企业", industry="水泥", region="华北")
    c3 = Company(code="X-001", name="第三方企业", industry="化工", region="华南")
    db.add_all([c1, c2, c3])
    db.flush()
    db.add_all([
        User(username="lender", display_name="出借方", role="enterprise",
             company_id=c1.id, password_hash=pwd, salt=salt),
        User(username="borrower", display_name="借入方", role="enterprise",
             company_id=c2.id, password_hash=pwd, salt=salt),
        User(username="other", display_name="第三方", role="enterprise",
             company_id=c3.id, password_hash=pwd, salt=salt),
        User(username="admin", display_name="监管员", role="admin",
             password_hash=pwd, salt=salt),
        User(username="verifier", display_name="核查员", role="verifier",
             password_hash=pwd, salt=salt),
    ])
    allocate_quota(db, c1.id, YEAR, baseline=1000, allocation_amount=1000)
    allocate_quota(db, c2.id, YEAR, baseline=100, allocation_amount=100)
    allocate_quota(db, c3.id, YEAR, baseline=500, allocation_amount=500)
    db.commit()
    ids = {"lender": c1.id, "borrower": c2.id, "other": c3.id}
    db.close()
    return ids


@pytest.fixture()
def ctx():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TestingSession = sessionmaker(bind=engine, autoflush=False)
    Base.metadata.create_all(engine)

    def override_get_db():
        s = TestingSession()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = override_get_db
    ids = _seed_db(TestingSession)
    client = TestClient(app)
    yield client, ids
    app.dependency_overrides.clear()
    engine.dispose()


@pytest.fixture()
def file_ctx(tmp_path):
    """文件型多连接库：HTTP 并发请求各自持有连接，真实复现并发语义。"""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'loan_api.db'}",
        connect_args={"check_same_thread": False, "timeout": 30},
    )

    @event.listens_for(engine, "connect")
    def _busy_timeout(dbapi_conn, _rec):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA busy_timeout=30000")
        cur.close()

    TestingSession = sessionmaker(bind=engine, autoflush=False)
    Base.metadata.create_all(engine)

    def override_get_db():
        s = TestingSession()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = override_get_db
    ids = _seed_db(TestingSession)
    client = TestClient(app)
    yield client, ids
    app.dependency_overrides.clear()
    engine.dispose()


def login(client, username):
    client.post("/api/auth/login", json={"username": username, "password": "123456"})


def _due(days=7):
    from datetime import datetime, timedelta
    return (datetime.utcnow() + timedelta(days=days)).strftime("%Y-%m-%d")


def _past_due():
    from datetime import datetime, timedelta
    return (datetime.utcnow() - timedelta(days=1)).strftime("%Y-%m-%d")


def _create_loan(client, ids, amount=200, due=None, initiator="lender",
                 username="lender", **extra):
    body = {
        "lender_id": ids["lender"],
        "borrower_id": ids["borrower"],
        "year": YEAR,
        "amount": amount,
        "due_date": due or _due(),
        "initiator": initiator,
        **extra,
    }
    res = client.post("/api/loans", json=body)
    assert res.status_code == 200, res.text
    return res.json()


# --------------------------------------------------------------------------- #
# 创建 / 确认 / 权限
# --------------------------------------------------------------------------- #

def test_create_loan_party_binding(ctx):
    client, ids = ctx
    # 出借方挂单：非出借企业（借入方）不能代发
    login(client, "borrower")
    res = client.post("/api/loans", json={
        "lender_id": ids["lender"], "borrower_id": ids["borrower"],
        "year": YEAR, "amount": 100, "due_date": _due(), "initiator": "lender",
    })
    assert res.status_code == 403
    # 借入方求借：合法
    loan = _create_loan(client, ids, amount=100, initiator="borrower",
                        username="borrower")
    assert loan["borrower_confirmed"] is True and loan["lender_confirmed"] is False


def test_create_loan_validation(ctx):
    client, ids = ctx
    login(client, "lender")
    res = client.post("/api/loans", json={
        "lender_id": ids["lender"], "borrower_id": ids["borrower"],
        "year": YEAR, "amount": -5, "due_date": _due(),
    })
    assert res.status_code == 422  # pydantic 校验
    res = client.post("/api/loans", json={
        "lender_id": ids["lender"], "borrower_id": ids["borrower"],
        "year": YEAR, "amount": 10, "due_date": "",
    })
    assert res.status_code in (400, 422)


def test_full_lifecycle_admin_driven(ctx):
    """出借发起 → 借入确认 → 监管批准冻结 → 监管拨付 → 借入归还 → 结清。"""
    client, ids = ctx
    login(client, "lender")
    loan = _create_loan(client, ids, amount=300)
    login(client, "borrower")
    res = client.post(f"/api/loans/{loan['id']}/confirm")
    assert res.status_code == 200 and res.json()["lender_confirmed"] is True

    # 企业不能批准
    res = client.post(f"/api/loans/{loan['id']}/approve")
    assert res.status_code == 403
    login(client, "admin")
    res = client.post(f"/api/loans/{loan['id']}/approve")
    assert res.status_code == 200 and res.json()["status"] == "reserved"
    res = client.post(f"/api/loans/{loan['id']}/disburse")
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "active"
    assert body["outstanding_principal"] == 300

    # 出借方不能归还
    login(client, "lender")
    res = client.post(f"/api/loans/{loan['id']}/repay", json={"amount": 300})
    assert res.status_code == 403
    # 借入方分次归还
    login(client, "borrower")
    res = client.post(f"/api/loans/{loan['id']}/repay", json={"amount": 120})
    assert res.status_code == 200 and res.json()["loan"]["status"] == "active"
    res = client.post(f"/api/loans/{loan['id']}/repay", json={"amount": 180})
    assert res.status_code == 200 and res.json()["loan"]["status"] == "repaid"


def test_cannot_approve_before_both_confirmed(ctx):
    client, ids = ctx
    login(client, "lender")
    loan = _create_loan(client, ids, amount=50)
    login(client, "admin")
    res = client.post(f"/api/loans/{loan['id']}/approve")
    assert res.status_code == 400 and "确认" in res.json()["detail"]


def test_lender_free_insufficient_blocks_approve(ctx):
    client, ids = ctx
    login(client, "lender")
    loan = _create_loan(client, ids, amount=1200)  # 超过 1000 持有
    login(client, "borrower")
    client.post(f"/api/loans/{loan['id']}/confirm")
    login(client, "admin")
    res = client.post(f"/api/loans/{loan['id']}/approve")
    assert res.status_code == 400 and "自由可用" in res.json()["detail"]


def test_reject_workflow_and_enterprise_forbidden(ctx):
    client, ids = ctx
    login(client, "lender")
    loan = _create_loan(client, ids, amount=10)
    login(client, "borrower")
    # 企业驳回 403 且写越权审计
    res = client.post(f"/api/loans/{loan['id']}/reject", json={"reason": "企业不能驳回"})
    assert res.status_code == 403
    login(client, "admin")
    res = client.post(f"/api/loans/{loan['id']}/reject", json={"reason": "材料不齐"})
    assert res.status_code == 200 and res.json()["status"] == "rejected"
    # 已驳回不能再批准
    res = client.post(f"/api/loans/{loan['id']}/approve")
    assert res.status_code == 400
    # 越权驳回已留痕
    res = client.get("/api/loans/audit-logs")
    assert res.status_code == 200
    assert any(log["result"] == "denied" for log in res.json())


def test_party_cancel_then_regulator_cancel_reserved(ctx):
    client, ids = ctx
    login(client, "lender")
    loan = _create_loan(client, ids, amount=30)
    login(client, "borrower")
    res = client.post(f"/api/loans/{loan['id']}/cancel", json={"reason": "协商取消"})
    assert res.status_code == 200 and res.json()["status"] == "cancelled"

    # 第二张：监管批准后企业撤不了、监管可撤并释放
    login(client, "lender")
    loan2 = _create_loan(client, ids, amount=40)
    login(client, "borrower")
    client.post(f"/api/loans/{loan2['id']}/confirm")
    login(client, "admin")
    client.post(f"/api/loans/{loan2['id']}/approve")
    login(client, "borrower")
    res = client.post(f"/api/loans/{loan2['id']}/cancel", json={"reason": "企业尝试"})
    assert res.status_code == 403
    login(client, "admin")
    res = client.post(f"/api/loans/{loan2['id']}/cancel", json={"reason": "监管叫停"})
    assert res.status_code == 200 and res.json()["status"] == "cancelled"


def test_list_isolation_and_detail_forbidden(ctx):
    client, ids = ctx
    login(client, "lender")
    _create_loan(client, ids, amount=10)
    # 第三方企业看不到
    login(client, "other")
    assert client.get("/api/loans").json() == []
    res = client.get("/api/loans/1")
    assert res.status_code == 403
    # 借入方能看到
    login(client, "borrower")
    assert len(client.get("/api/loans").json()) == 1
    # 核查员能看全部
    login(client, "verifier")
    assert len(client.get("/api/loans").json()) == 1
    # 企业读审计 403 并留痕
    login(client, "borrower")
    res = client.get("/api/loans/audit-logs")
    assert res.status_code == 403


def test_verifier_cannot_mutate(ctx):
    client, ids = ctx
    login(client, "lender")
    loan = _create_loan(client, ids, amount=10)
    login(client, "verifier")
    for path, method, body in [
        (f"/api/loans/{loan['id']}/approve", "POST", None),
        (f"/api/loans/{loan['id']}/disburse", "POST", None),
        (f"/api/loans/{loan['id']}/reject", "POST", {"reason": "核查员无权"}),
        ("/api/loans/mark-overdue?year=2026", "POST", None),
    ]:
        res = client.request(method, path, json=body)
        assert res.status_code == 403, path


# --------------------------------------------------------------------------- #
# 逾期 / 违约 / 追偿
# --------------------------------------------------------------------------- #

def _active_past_due_loan(client, ids, amount=300):
    login(client, "lender")
    loan = _create_loan(client, ids, amount=amount, due=_past_due())
    login(client, "borrower")
    client.post(f"/api/loans/{loan['id']}/confirm")
    login(client, "admin")
    client.post(f"/api/loans/{loan['id']}/approve")
    client.post(f"/api/loans/{loan['id']}/disburse")
    return loan["id"]


def test_overdue_default_recover_cycle(ctx):
    client, ids = ctx
    loan_id = _active_past_due_loan(client, ids, amount=300)
    login(client, "admin")
    # 未逾期标记前不能宣告违约
    res = client.post(f"/api/loans/{loan_id}/default", json={"reason": "逾期未还"})
    assert res.status_code == 400
    # 标记逾期（幂等）
    res = client.post(f"/api/loans/mark-overdue?year=2026")
    assert res.status_code == 200 and res.json()["marked_count"] == 1
    assert client.post("/api/loans/mark-overdue?year=2026").json()["marked_count"] == 0
    # 借入方当前 400（100 自有 + 300 到账）且无冻结/占用，宣告违约应足额收回 → repaid
    res = client.post(f"/api/loans/{loan_id}/default", json={"reason": "到期未清偿"})
    assert res.status_code == 200
    assert res.json()["clawback"]["recovered"] == 300
    assert res.json()["status"] == "repaid"


def test_partial_default_then_manual_recovery(ctx):
    """把借入方自由可用压到 80：宣告违约收回 80、登记欠额 220，再买入后追偿结清。"""
    client, ids = ctx
    loan_id = _active_past_due_loan(client, ids, amount=300)
    db = next(app.dependency_overrides[get_db]())
    borrower_acc = db.query(AllowanceAccount).filter_by(
        company_id=ids["borrower"], year=YEAR).one()
    from app.core.ledger import apply_ledger_delta, lock_row_for_write, transactional
    with transactional(db):
        lock_row_for_write(db, borrower_acc.id)
        apply_ledger_delta(db, borrower_acc.id, 0, 0, 320)
    db.close()

    login(client, "admin")
    client.post("/api/loans/mark-overdue?year=2026")
    res = client.post(f"/api/loans/{loan_id}/default", json={"reason": "到期未清偿"})
    assert res.status_code == 200
    assert res.json()["clawback"]["recovered"] == 80
    assert res.json()["clawback"]["defaulted"] == 220
    assert res.json()["status"] == "defaulted"
    loan_obj = db.get(QuotaLoan, loan_id)
    assert loan_obj.status == "defaulted"

    # 无可用追偿 → 400
    res = client.post(f"/api/loans/defaults/{ids['borrower']}/recover?year=2026")
    assert res.status_code == 400
    # 外部买入 220 后手动追偿
    db = next(app.dependency_overrides[get_db]())
    borrower_acc = db.query(AllowanceAccount).filter_by(
        company_id=ids["borrower"], year=YEAR).one()
    with transactional(db):
        apply_ledger_delta(db, borrower_acc.id, 220, 0, 0)
    db.close()
    res = client.post(f"/api/loans/defaults/{ids['borrower']}/recover?year=2026")
    assert res.status_code == 200 and res.json()["recovered_volume"] == 220
    detail = client.get(f"/api/loans/{loan_id}").json()
    assert detail["status"] == "repaid"
    assert detail["default_outstanding"] == 0


def test_defaults_list_isolation(ctx):
    client, ids = ctx
    _active_past_due_loan(client, ids, amount=300)
    login(client, "admin")
    client.post("/api/loans/mark-overdue?year=2026")
    client.post(f"/api/loans/1/default", json={"reason": "x" * 2})
    # 监管看全部欠额
    defaults = client.get("/api/loans/defaults?year=2026").json()
    # 足额收回即 repaid，无欠额；先验证隔离接口可用
    assert isinstance(defaults, list)
    login(client, "other")
    assert client.get("/api/loans/defaults?year=2026").json() == []


# --------------------------------------------------------------------------- #
# 幂等 / HTTP 并发
# --------------------------------------------------------------------------- #

def test_create_loan_idempotent_key(ctx):
    client, ids = ctx
    login(client, "lender")
    body = {
        "lender_id": ids["lender"], "borrower_id": ids["borrower"],
        "year": YEAR, "amount": 77, "due_date": _due(),
        "idempotency_key": "loan-idem-1",
    }
    r1 = client.post("/api/loans", json=body)
    r2 = client.post("/api/loans", json=body)
    assert r1.json()["id"] == r2.json()["id"]
    assert len(client.get("/api/loans").json()) == 1


def test_concurrent_repay_idempotent_and_never_overpay(file_ctx):
    """同幂等键并发两次归还只入账一次；无键并发累计也不超过本金。"""
    from concurrent.futures import ThreadPoolExecutor

    client, ids = file_ctx
    # 建 300 借据并拨付
    login(client, "lender")
    res = client.post("/api/loans", json={
        "lender_id": ids["lender"], "borrower_id": ids["borrower"],
        "year": YEAR, "amount": 300, "due_date": _due(),
        "idempotency_key": "create-concurrent",
    })
    loan_id = res.json()["id"]
    login(client, "borrower")
    client.post(f"/api/loans/{loan_id}/confirm")
    login(client, "admin")
    client.post(f"/api/loans/{loan_id}/approve")
    client.post(f"/api/loans/{loan_id}/disburse")

    login(client, "borrower")

    def repay_same_key(_):
        c = TestClient(app)
        c.post("/api/auth/login", json={"username": "borrower", "password": "123456"})
        r = c.post(
            f"/api/loans/{loan_id}/repay",
            json={"amount": 100, "idempotency_key": "repay-same"},
        )
        return r.status_code

    with ThreadPoolExecutor(max_workers=4) as pool:
        codes = list(pool.map(repay_same_key, range(4)))
    assert all(c == 200 for c in codes)

    # 无幂等键并发归还剩余 200，多请求竞争后累计不得超过本金
    def repay_any(_):
        c = TestClient(app)
        c.post("/api/auth/login", json={"username": "borrower", "password": "123456"})
        return c.post(f"/api/loans/{loan_id}/repay", json={"amount": 100}).status_code

    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(repay_any, range(6)))

    detail = client.get(f"/api/loans/{loan_id}").json()
    assert detail["returned_amount"] == 300
    assert detail["status"] == "repaid"
    db = next(app.dependency_overrides[get_db]())
    lender = db.query(AllowanceAccount).filter_by(
        company_id=ids["lender"], year=YEAR).one()
    borrower = db.query(AllowanceAccount).filter_by(
        company_id=ids["borrower"], year=YEAR).one()
    # 出借方：1000 − 300 拨付 + 300 归还 = 1000；借入方：100 + 300 − 300 = 100
    assert float(lender.current_balance) == 1000
    assert float(borrower.current_balance) == 100
    db.close()


def test_disburse_with_auto_clear_clears_deficit(ctx):
    """拨付即清缴闭环：借入方有履约缺口时，到账配额同事务补缴并达标。"""
    client, ids = ctx
    db = next(app.dependency_overrides[get_db]())
    from app.models import ActivityData, CalculationMethod, EmissionFactor, EmissionScope
    from app.services.calculation_service import recalc_company_year
    from app.services.mrv_service import approve_report, generate_report, submit_report

    cid = ids["borrower"]
    db.add(EmissionScope(company_id=cid, scope="2", category="外购电力", name="厂区用电"))
    db.add(CalculationMethod(method_code="ELEC", name="外购电力排放因子法", scope="2",
                             formula_type="activity_factor"))
    db.add(EmissionFactor(factor_code="ELEC-GRID", name="外购电力", scope="2",
                          unit="tCO2/MWh", value=0.5, source="电网因子",
                          valid_from="2025-01-01", valid_to="2027-12-31"))
    db.flush()
    sid = db.query(EmissionScope).filter_by(company_id=cid, scope="2").one().id
    db.add(ActivityData(company_id=cid, scope_id=sid, year=YEAR, period="monthly",
                        activity_type="外购电力", unit="MWh", quantity=400,
                        data_source="台账", verified=1))
    db.commit()
    recalc_company_year(db, cid, YEAR)
    report = generate_report(db, cid, YEAR)
    submit_report(db, report)
    approve_report(db, report, verifier_id=1)
    db.close()

    login(client, "lender")
    res = client.post("/api/loans", json={
        "lender_id": ids["lender"], "borrower_id": ids["borrower"],
        "year": YEAR, "amount": 200, "due_date": _due(),
    })
    loan_id = res.json()["id"]
    login(client, "borrower")
    client.post(f"/api/loans/{loan_id}/confirm")
    login(client, "admin")
    res = client.post(f"/api/loans/{loan_id}/disburse")
    # 未批准不能拨付
    assert res.status_code == 400
    client.post(f"/api/loans/{loan_id}/approve")
    res = client.post(f"/api/loans/{loan_id}/disburse")
    assert res.status_code == 200
    # 借入方履约记录达标
    db = next(app.dependency_overrides[get_db]())
    from app.models import ComplianceRecord
    record = db.query(ComplianceRecord).filter_by(
        company_id=cid, year=YEAR, is_active=1).one()
    assert record.status == "compliant"
    assert float(record.cleared_amount) == 200
    db.close()
