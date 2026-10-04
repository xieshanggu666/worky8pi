"""配额交易明细越权回归测试（IDOR / 水平越权）。

覆盖：
- enterprise 只能访问本企业账户与交易明细，遍历他人 account_id 一律 403；
- 不存在的账户返回 404，且不泄露归属信息；
- admin / verifier 监管侧角色可跨企业查看；
- 未登录 401；
- 交易划转接口同样具备账户归属校验。
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.database import Base, get_db
from app.core.security import hash_password
from app.main import app
from app.models import AllowanceAccount, AllowanceTransaction, Company, User


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

    u1 = User(username="ent1", display_name="企业甲用户", role="enterprise",
              company_id=c1.id, password_hash=pwd_hash, salt=salt)
    u2 = User(username="ent2", display_name="企业乙用户", role="enterprise",
              company_id=c2.id, password_hash=pwd_hash, salt=salt)
    admin = User(username="admin", display_name="监管员", role="admin",
                 password_hash=pwd_hash, salt=salt)
    verifier = User(username="verifier", display_name="核查员", role="verifier",
                    password_hash=pwd_hash, salt=salt)
    db.add_all([u1, u2, admin, verifier])
    db.flush()

    # 两家企业各自的 2025 年度账户，各一笔分配流水
    acc1 = AllowanceAccount(company_id=c1.id, year=2025, opening_balance=1000, current_balance=1000)
    acc2 = AllowanceAccount(company_id=c2.id, year=2025, opening_balance=500, current_balance=500)
    db.add_all([acc1, acc2])
    db.flush()
    db.add_all([
        AllowanceTransaction(account_id=acc1.id, company_id=c1.id, tx_type="allocation",
                             amount=1000, counterparty="主管部门", tx_date="2025-01-01",
                             balance_after=1000, remark="甲企业配额分配"),
        AllowanceTransaction(account_id=acc2.id, company_id=c2.id, tx_type="allocation",
                             amount=500, counterparty="主管部门", tx_date="2025-01-01",
                             balance_after=500, remark="乙企业配额分配"),
    ])
    db.commit()
    ids = {
        "company1": c1.id, "company2": c2.id,
        "account1": acc1.id, "account2": acc2.id,
    }
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


# ---------- 交易明细：水平越权防护 ----------

def test_tx_list_own_account(ctx):
    client, ids = ctx
    login(client, "ent1")
    res = client.get(f"/api/accounts/{ids['account1']}/transactions")
    assert res.status_code == 200
    data = res.json()
    assert len(data) == 1
    assert data[0]["remark"] == "甲企业配额分配"


def test_tx_list_cross_company_forbidden(ctx):
    """核心回归：企业甲遍历到企业乙的 account_id，必须 403，不得返回其交易数据。"""
    client, ids = ctx
    login(client, "ent1")
    res = client.get(f"/api/accounts/{ids['account2']}/transactions")
    assert res.status_code == 403
    assert "乙企业" not in res.text
    assert "500" not in res.text  # 不得泄露对方余额/交易量


def test_tx_list_missing_account_404(ctx):
    client, _ = ctx
    login(client, "ent1")
    res = client.get("/api/accounts/999999/transactions")
    assert res.status_code == 404


def test_tx_list_admin_cross_company_ok(ctx):
    client, ids = ctx
    login(client, "admin")
    res = client.get(f"/api/accounts/{ids['account2']}/transactions")
    assert res.status_code == 200
    assert res.json()[0]["remark"] == "乙企业配额分配"


def test_tx_list_verifier_cross_company_ok(ctx):
    client, ids = ctx
    login(client, "verifier")
    res = client.get(f"/api/accounts/{ids['account1']}/transactions")
    assert res.status_code == 200


def test_tx_list_requires_login(ctx):
    client, ids = ctx
    res = client.get(f"/api/accounts/{ids['account1']}/transactions")
    assert res.status_code == 401


# ---------- 账户查询 / 划转：同一归属边界 ----------

def test_account_cross_company_forbidden(ctx):
    client, ids = ctx
    login(client, "ent1")
    res = client.get(f"/api/companies/{ids['company2']}/account?year=2025")
    assert res.status_code == 403


def test_account_own_ok(ctx):
    client, ids = ctx
    login(client, "ent1")
    res = client.get(f"/api/companies/{ids['company1']}/account?year=2025")
    assert res.status_code == 200
    assert res.json()["current_balance"] == 1000.0


def test_transfer_cross_company_forbidden(ctx):
    client, ids = ctx
    login(client, "ent1")
    res = client.post(
        f"/api/accounts/{ids['account2']}/transfer",
        json={"amount": 10, "tx_type": "sell", "counterparty": "x",
              "price": 1, "tx_date": "2025-06-01", "remark": "越权划转"},
    )
    assert res.status_code == 403


def test_report_reverse_requires_regulator_role(ctx):
    """企业用户不能冲正已批准报告；核查角色可进入业务校验（不存在则 404）。"""
    client, _ = ctx
    login(client, "ent1")
    res = client.post("/api/reports/999999/reverse", json={"reason": "越权冲正测试"})
    assert res.status_code == 403

    login(client, "verifier")
    res = client.post("/api/reports/999999/reverse", json={"reason": "记录不存在"})
    assert res.status_code == 404
