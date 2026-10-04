"""企业间交易订单 API 集成测试：鉴权边界、状态流转、双方账户同步。"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.database import Base, get_db
from app.core.security import hash_password
from app.main import app
from app.models import Company, User
from app.services.quota_service import allocate_quota

PENDING, CONFIRMED, DELIVERED = "pending", "confirmed", "delivered"


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

    c1 = Company(code="E-001", name="企业甲", industry="电力", region="华东")
    c2 = Company(code="E-002", name="企业乙", industry="水泥", region="华北")
    db.add_all([c1, c2])
    db.flush()

    u1 = User(username="ent1", display_name="甲", role="enterprise",
              company_id=c1.id, password_hash=pwd_hash, salt=salt)
    u2 = User(username="ent2", display_name="乙", role="enterprise",
              company_id=c2.id, password_hash=pwd_hash, salt=salt)
    admin = User(username="admin", display_name="监管员", role="admin",
                 password_hash=pwd_hash, salt=salt)
    verifier = User(username="verifier", display_name="核查员", role="verifier",
                    password_hash=pwd_hash, salt=salt)
    db.add_all([u1, u2, admin, verifier])
    db.flush()

    allocate_quota(db, c1.id, 2025, baseline=1000, allocation_amount=1000)
    allocate_quota(db, c2.id, 2025, baseline=400, allocation_amount=400)
    db.commit()
    ids = {"c1": c1.id, "c2": c2.id}
    db.close()

    def override_get_db():
        session = TestingSession()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = override_get_db
    client = TestClient(app)
    yield client, ids
    app.dependency_overrides.clear()
    engine.dispose()


def login(client, username):
    client.post("/api/auth/login", json={"username": username, "password": "123456"})


def test_create_order_flow_by_two_enterprises(ctx):
    """卖方企业挂单 → 买方确认 → 卖方交割，双方余额与状态同步。"""
    client, ids = ctx
    login(client, "ent1")
    res = client.post("/api/trade-orders", json={
        "seller_id": ids["c1"], "buyer_id": ids["c2"], "year": 2025,
        "amount": 300, "price": 80, "initiator": "seller",
    }, headers={"Idempotency-Key": "api-key-1"})
    assert res.status_code == 200
    order = res.json()
    assert order["status"] == PENDING
    assert order["seller_confirmed"] is True
    assert order["buyer_confirmed"] is False
    oid = order["id"]

    # 重复挂单（同幂等键）返回同一订单
    again = client.post("/api/trade-orders", json={
        "seller_id": ids["c1"], "buyer_id": ids["c2"], "year": 2025,
        "amount": 999, "price": 80,
    }, headers={"Idempotency-Key": "api-key-1"})
    assert again.json()["id"] == oid

    # 买方确认
    login(client, "ent2")
    res = client.post(f"/api/trade-orders/{oid}/confirm")
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == CONFIRMED
    # 回归：autoflush=False 下双方确认标志都必须跨请求持久化
    assert body["seller_confirmed"] is True
    assert body["buyer_confirmed"] is True
    detail = client.get(f"/api/trade-orders/{oid}").json()
    assert detail["seller_confirmed"] is True
    assert detail["buyer_confirmed"] is True

    # 卖方账户：1000 持仓中 300 被交易占用（切回卖方会话查询本企业账户）
    login(client, "ent1")
    res = client.get(f"/api/companies/{ids['c1']}/account?year=2025")
    acc = res.json()
    assert acc["current_balance"] == 1000
    assert acc["reserved_balance"] == 300
    assert acc["available_balance"] == 700

    # 交割（买卖双方均可发起）
    res = client.post(f"/api/trade-orders/{oid}/deliver")
    assert res.status_code == 200
    assert res.json()["status"] == DELIVERED

    login(client, "ent2")
    res = client.get(f"/api/companies/{ids['c2']}/account?year=2025")
    assert res.json()["current_balance"] == 700
    login(client, "ent1")
    res = client.get(f"/api/companies/{ids['c1']}/account?year=2025")
    assert res.json()["current_balance"] == 700


def test_enterprise_cannot_create_order_on_behalf_of_others(ctx):
    """企业只能以本企业名义挂单：卖方挂单时 seller_id 必须是自己。"""
    client, ids = ctx
    login(client, "ent2")
    res = client.post("/api/trade-orders", json={
        "seller_id": ids["c1"], "buyer_id": ids["c2"], "year": 2025, "amount": 100,
    })
    assert res.status_code == 403

    # 买方求购时 buyer_id 必须是自己；ent2 作为买方是合法的
    res = client.post("/api/trade-orders", json={
        "seller_id": ids["c1"], "buyer_id": ids["c2"], "year": 2025,
        "amount": 100, "initiator": "buyer",
    })
    assert res.status_code == 200


def test_enterprise_order_list_scoped_to_parties(ctx):
    """企业只能看到自己参与的订单；admin 可见全部。"""
    client, ids = ctx
    # 用 service 直接造一张甲→乙订单，再造第三家企业验证隔离
    from app.models import Company, User
    from app.core.database import SessionLocal

    login(client, "admin")
    res = client.post("/api/trade-orders", json={
        "seller_id": ids["c1"], "buyer_id": ids["c2"], "year": 2025, "amount": 100,
    })
    oid = res.json()["id"]

    login(client, "ent1")
    res = client.get("/api/trade-orders")
    assert res.status_code == 200
    assert len(res.json()) == 1

    # 监管核查角色可查看全部订单，但不能代为确认（仅 admin/enterprise 可写操作）
    login(client, "verifier")
    res = client.get("/api/trade-orders")
    assert len(res.json()) == 1
    res = client.post(f"/api/trade-orders/{oid}/confirm")
    assert res.status_code == 403


def test_non_party_enterprise_forbidden(ctx):
    """与订单无关的企业不能确认/撤销/交割。"""
    client, ids = ctx
    # admin 建单
    login(client, "admin")
    oid = client.post("/api/trade-orders", json={
        "seller_id": ids["c1"], "buyer_id": ids["c2"], "year": 2025, "amount": 100,
    }).json()["id"]

    # ent2 是买方合法；再造一个第三方用户验证拒绝
    from app.models import Company, User

    db = next(app.dependency_overrides[get_db]())
    c3 = Company(code="E-003", name="企业丙", industry="化工", region="华南")
    db.add(c3)
    db.flush()
    h, s = hash_password("123456")
    db.add(User(username="ent3", display_name="丙", role="enterprise",
                company_id=c3.id, password_hash=h, salt=s))
    allocate_quota(db, c3.id, 2025, baseline=100, allocation_amount=100)
    db.commit()
    db.close()

    login(client, "ent3")
    assert client.post(f"/api/trade-orders/{oid}/confirm").status_code == 403
    assert client.post(f"/api/trade-orders/{oid}/deliver").status_code == 403
    assert client.post(f"/api/trade-orders/{oid}/cancel", json={"reason": "x"}).status_code == 403
    assert client.get(f"/api/trade-orders/{oid}").status_code == 403


def test_buyer_initiated_seller_confirms_persists_flags(ctx):
    """买方求购 → 卖方确认（反向确认）：双方标志同样正确落库并占用配额。"""
    client, ids = ctx
    login(client, "ent2")
    oid = client.post("/api/trade-orders", json={
        "seller_id": ids["c1"], "buyer_id": ids["c2"], "year": 2025,
        "amount": 200, "initiator": "buyer",
    }).json()["id"]
    login(client, "ent1")
    body = client.post(f"/api/trade-orders/{oid}/confirm").json()
    assert body["status"] == CONFIRMED
    assert body["seller_confirmed"] is True
    assert body["buyer_confirmed"] is True
    acc = client.get(f"/api/companies/{ids['c1']}/account?year=2025").json()
    assert acc["reserved_balance"] == 200


def test_cancel_confirmed_releases_reservation(ctx):
    """确认后撤销：占用释放回可用，流水可见。"""
    client, ids = ctx
    login(client, "ent1")
    oid = client.post("/api/trade-orders", json={
        "seller_id": ids["c1"], "buyer_id": ids["c2"], "year": 2025, "amount": 300,
    }).json()["id"]
    login(client, "ent2")
    client.post(f"/api/trade-orders/{oid}/confirm")
    res = client.post(f"/api/trade-orders/{oid}/cancel", json={"reason": "协商终止"})
    assert res.status_code == 200
    assert res.json()["status"] == "cancelled"

    login(client, "ent1")
    acc = client.get(f"/api/companies/{ids['c1']}/account?year=2025").json()
    assert acc["reserved_balance"] == 0
    assert acc["available_balance"] == 1000


def test_deliver_unconfirmed_rejected(ctx):
    """未经双方确认不能交割。"""
    client, ids = ctx
    login(client, "ent1")
    oid = client.post("/api/trade-orders", json={
        "seller_id": ids["c1"], "buyer_id": ids["c2"], "year": 2025, "amount": 300,
    }).json()["id"]
    res = client.post(f"/api/trade-orders/{oid}/deliver")
    assert res.status_code == 400
    assert "双方确认" in res.json()["detail"]


def test_missing_order_404(ctx):
    client, _ = ctx
    login(client, "admin")
    assert client.get("/api/trade-orders/999999").status_code == 404
    assert client.post("/api/trade-orders/999999/confirm").status_code == 404


def test_order_requires_login(ctx):
    client, _ = ctx
    client.cookies.clear()
    assert client.get("/api/trade-orders").status_code == 401
    assert client.post("/api/trade-orders", json={
        "seller_id": 1, "buyer_id": 2, "year": 2025, "amount": 10,
    }).status_code == 401


def test_list_orders_filter_by_status_and_year(ctx):
    client, ids = ctx
    login(client, "admin")
    client.post("/api/trade-orders", json={
        "seller_id": ids["c1"], "buyer_id": ids["c2"], "year": 2025, "amount": 100,
    })
    res = client.get("/api/trade-orders?year=2025&status=pending")
    assert len(res.json()) == 1
    assert client.get("/api/trade-orders?status=delivered").json() == []
    assert client.get("/api/trade-orders?year=2024").json() == []


def _setup_buyer_deficit(c2_id, emission, factor=0.5703):
    """通过 service 层构造买方年度缺口：活动数据→核算→报告批准（冻结+缺口）。"""
    from app.models import ActivityData, CalculationMethod, EmissionFactor, EmissionScope
    from app.core.database import SessionLocal
    from app.services.calculation_service import recalc_company_year
    from app.services.mrv_service import approve_report, generate_report, submit_report

    db = next(app.dependency_overrides[get_db]())
    db.add(EmissionScope(company_id=c2_id, scope="2", category="外购电力", name="厂区用电"))
    db.add(CalculationMethod(
        method_code="ELEC", name="外购电力排放因子法", scope="2", formula_type="activity_factor"))
    db.add(EmissionFactor(
        factor_code="ELEC-GRID", name="外购电力", scope="2", unit="tCO2/MWh", value=factor,
        source="电网因子", valid_from="2024-01-01", valid_to="2025-12-31"))
    db.flush()
    scope_id = db.query(EmissionScope).filter_by(company_id=c2_id, scope="2").one().id
    db.add(ActivityData(
        company_id=c2_id, scope_id=scope_id, year=2025, period="monthly",
        activity_type="外购电力", unit="MWh",
        quantity=round(emission / factor, 6), data_source="台账", verified=1))
    db.commit()
    recalc_company_year(db, c2_id, 2025)
    report = generate_report(db, c2_id, 2025)
    submit_report(db, report)
    approve_report(db, report, verifier_id=1)
    db.commit()
    db.close()


def test_deliver_api_returns_buyer_clearance_and_closes_loop(ctx):
    """交割接口返回买方履约核销结果，余额/履约/统计经 API 全部闭环。"""
    client, ids = ctx
    # 买方排放 600：配额 400 全冻结，缺口 200
    _setup_buyer_deficit(ids["c2"], 600)

    login(client, "ent1")
    oid = client.post("/api/trade-orders", json={
        "seller_id": ids["c1"], "buyer_id": ids["c2"], "year": 2025, "amount": 200,
    }).json()["id"]
    login(client, "ent2")
    client.post(f"/api/trade-orders/{oid}/confirm")
    login(client, "ent1")
    res = client.post(f"/api/trade-orders/{oid}/deliver")
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == DELIVERED
    assert body["auto_clear_deficit"] is True
    clearance = body["buyer_clearance"]
    assert clearance is not None
    assert clearance["status"] == "compliant"
    assert clearance["cleared_amount"] == 600
    assert clearance["frozen_amount"] == 0
    assert clearance["deficit"] == 0

    # 买方账户：400 + 200 - 600 清缴 = 0
    login(client, "ent2")
    acc = client.get(f"/api/companies/{ids['c2']}/account?year=2025").json()
    assert acc["current_balance"] == 0
    assert acc["frozen_balance"] == 0
    compliance = client.get("/api/compliance?year=2025").json()
    assert len(compliance) == 1
    assert compliance[0]["status"] == "compliant"

    # 仪表盘（企业视角）同步：已清缴 600、持仓 0、达标 1 家
    stats = client.get("/api/dashboard/stats?year=2025").json()
    assert stats["cleared_total"] == 600
    assert stats["current_balance_total"] == 0
    assert stats["compliance_counts"]["compliant"] == 1


def test_create_order_with_auto_clear_disabled(ctx):
    """挂单可显式关闭交割联动清缴；该订单交割后买方缺口保留。"""
    client, ids = ctx
    _setup_buyer_deficit(ids["c2"], 600)

    login(client, "admin")
    oid = client.post("/api/trade-orders", json={
        "seller_id": ids["c1"], "buyer_id": ids["c2"], "year": 2025,
        "amount": 200, "auto_clear_deficit": False,
    }).json()["id"]
    detail = client.get(f"/api/trade-orders/{oid}").json()
    assert detail["auto_clear_deficit"] is False
    login(client, "ent2")
    client.post(f"/api/trade-orders/{oid}/confirm")
    login(client, "admin")
    res = client.post(f"/api/trade-orders/{oid}/deliver")
    assert res.status_code == 200
    assert res.json()["buyer_clearance"]["status"] == "deficit"

    acc = client.get(f"/api/companies/{ids['c2']}/account?year=2025").json()
    # 到账 200 留存为自由可用，冻结 400 不动，缺口仍是 200
    assert acc["current_balance"] == 600
    assert acc["frozen_balance"] == 400
    assert acc["available_balance"] == 200
