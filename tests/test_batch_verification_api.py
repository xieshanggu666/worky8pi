"""批量核验 API 集成测试：角色边界、批量/单条接口、拦截与回滚响应。"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.database import Base, get_db
from app.core.security import hash_password
from app.main import app
from app.models import (
    ActivityData,
    CalculationMethod,
    Company,
    EmissionFactor,
    EmissionResult,
    EmissionScope,
    MrvReport,
    User,
)


@pytest.fixture()
def ctx():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TestingSession = sessionmaker(bind=engine, autoflush=False)
    Base.metadata.create_all(engine)

    db = TestingSession()
    pwd_hash, salt = hash_password("123456")
    c1 = Company(code="V-001", name="核验企业", industry="电力", region="华东")
    c2 = Company(code="V-002", name="无关企业", industry="水泥", region="华北")
    db.add_all([c1, c2])
    db.flush()
    scope = EmissionScope(company_id=c1.id, scope="2", category="外购电力", name="厂区用电")
    db.add(scope)
    db.flush()
    db.add(CalculationMethod(method_code="ELEC", name="外购电因子法", scope="2",
                             formula_type="activity_factor"))
    db.add(EmissionFactor(factor_code="ELEC-GRID", name="外购电力", scope="2",
                          unit="tCO2/MWh", value=0.5703, source="电网因子",
                          valid_from="2025-01-01", valid_to="2025-12-31"))
    db.add_all([
        User(username="ent", display_name="企业用户", role="enterprise",
             company_id=c1.id, password_hash=pwd_hash, salt=salt),
        User(username="admin", display_name="监管员", role="admin",
             password_hash=pwd_hash, salt=salt),
        User(username="verifier", display_name="核查员", role="verifier",
             password_hash=pwd_hash, salt=salt),
    ])

    def act(qty, verified=0, year=2025):
        a = ActivityData(company_id=c1.id, scope_id=scope.id, year=year, period="monthly",
                         activity_type="外购电力", unit="MWh", quantity=qty,
                         data_source="台账", verified=verified)
        db.add(a)
        return a

    acts = [act(1000), act(2000), act(3000, year=2024)]
    db.commit()
    ids = {"c1": c1.id, "c2": c2.id, "a": [a.id for a in acts]}
    db.close()

    def override_get_db():
        session = TestingSession()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = override_get_db
    client = TestClient(app)
    yield client, ids, TestingSession
    app.dependency_overrides.clear()
    engine.dispose()


def login(client, username):
    client.post("/api/auth/login", json={"username": username, "password": "123456"})


def test_batch_verify_requires_auth(ctx):
    client, _, _ = ctx
    res = client.post("/api/activity/batch-verify", json={"activity_ids": [1]})
    assert res.status_code == 401


def test_enterprise_cannot_batch_verify(ctx):
    client, ids, _ = ctx
    login(client, "ent")
    res = client.post("/api/activity/batch-verify", json={"activity_ids": ids["a"][:1]})
    assert res.status_code == 403


def test_verifier_batch_verify_by_ids(ctx):
    client, ids, Session = ctx
    login(client, "verifier")
    res = client.post("/api/activity/batch-verify", json={"activity_ids": ids["a"][:2]})
    assert res.status_code == 200, res.text
    data = res.json()
    assert data["verified_count"] == 2
    assert len(data["recalculated"]) == 1
    assert data["recalculated"][0]["result_count"] == 2
    assert data["remaining_unverified"][f"{ids['c1']}:2025"] == 0
    # 2024 年那条不受影响
    db = Session()
    assert db.get(ActivityData, ids["a"][0]).verified == 1
    assert db.get(ActivityData, ids["a"][2]).verified == 0
    assert db.query(EmissionResult).count() == 2
    db.close()


def test_admin_batch_verify_by_company_year(ctx):
    client, ids, Session = ctx
    login(client, "admin")
    res = client.post("/api/activity/batch-verify", json={"company_id": ids["c1"], "year": 2024})
    assert res.status_code == 200
    assert res.json()["verified_count"] == 1
    db = Session()
    assert db.get(ActivityData, ids["a"][2]).verified == 1
    db.close()


def test_batch_verify_missing_id_404(ctx):
    client, _, _ = ctx
    login(client, "verifier")
    res = client.post("/api/activity/batch-verify", json={"activity_ids": [404404]})
    assert res.status_code == 404
    assert "不存在" in res.json()["detail"]


def test_batch_verify_without_scope_400(ctx):
    client, _, _ = ctx
    login(client, "verifier")
    res = client.post("/api/activity/batch-verify", json={"company_id": 1})
    assert res.status_code == 400


def test_batch_verify_submitted_report_reset(ctx):
    """批量核验联动：已提交报告退回草稿并刷新快照。"""
    client, ids, Session = ctx
    login(client, "ent")
    client.post(f"/api/companies/{ids['c1']}/calculate?year=2025")
    client.post(f"/api/companies/{ids['c1']}/reports/generate?year=2025")
    reports = client.get(f"/api/companies/{ids['c1']}/reports").json()
    rid = reports[0]["id"]
    client.post(f"/api/reports/{rid}/submit")
    login(client, "verifier")
    res = client.post("/api/activity/batch-verify", json={"activity_ids": ids["a"][:2]})
    assert res.status_code == 200
    assert res.json()["reports"][0]["action"] == "reset_submitted"
    db = Session()
    report = db.get(MrvReport, rid)
    assert report.status == "draft"
    assert float(report.total_emission) > 0
    db.close()


def test_approved_year_rejects_batch_verify(ctx):
    """已批准（已冻结配额）年度：批量核验 400 且不改动数据。"""
    client, ids, Session = ctx
    db = Session()
    from app.services.quota_service import allocate_quota
    from app.services.calculation_service import recalc_company_year
    from app.services.mrv_service import approve_report, generate_report, submit_report as svc_submit

    for aid in ids["a"][:2]:
        db.get(ActivityData, aid).verified = 1
    db.commit()
    recalc_company_year(db, ids["c1"], 2025)
    allocate_quota(db, ids["c1"], 2025, baseline=10000, allocation_amount=10000)
    report = generate_report(db, ids["c1"], 2025)
    svc_submit(db, report)
    approve_report(db, report, verifier_id=1)
    db.close()

    login(client, "verifier")
    # 2024 年那条待核验数据属于未批准年度，可核验；2025 年度新增数据被拦截
    db = Session()
    extra = ActivityData(company_id=ids["c1"], scope_id=db.get(ActivityData, ids["a"][0]).scope_id,
                         year=2025, period="monthly", activity_type="外购电力", unit="MWh",
                         quantity=500, data_source="补录", verified=0)
    db.add(extra)
    db.commit()
    extra_id = extra.id
    db.close()

    res = client.post("/api/activity/batch-verify", json={"activity_ids": [extra_id]})
    assert res.status_code == 400
    assert "已批准" in res.json()["detail"]
    db = Session()
    assert db.get(ActivityData, extra_id).verified == 0
    db.close()


def test_single_verify_endpoint_runs_batch_pipeline(ctx):
    """单条核验接口保留兼容，并走批量内核（联动重算）。"""
    client, ids, Session = ctx
    login(client, "verifier")
    res = client.post(f"/api/activity/{ids['a'][0]}/verify")
    assert res.status_code == 200
    data = res.json()
    assert data["verified"] == 1
    assert data["verified_count"] == 1
    assert len(data["recalculated"]) == 1
    db = Session()
    assert db.query(EmissionResult).count() == 1
    db.close()


def test_single_verify_404(ctx):
    client, _, _ = ctx
    login(client, "admin")
    res = client.post("/api/activity/999999/verify")
    assert res.status_code == 404
