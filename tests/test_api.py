"""API 冒烟测试：登录、鉴权与仪表盘。"""

import pytest
from fastapi.testclient import TestClient

from app.core.database import Base, SessionLocal, engine
from app.core.security import hash_password
from app.main import app
from app.models import Company, User


@pytest.fixture(scope="module")
def client():
    Base.metadata.create_all(engine)
    db = SessionLocal()
    pwd_hash, salt = hash_password("123456")
    if not db.query(User).filter(User.username == "admin").first():
        db.add(User(username="admin", display_name="监管管理员", role="admin", password_hash=pwd_hash, salt=salt))
    if not db.query(Company).filter(Company.code == "API-001").first():
        db.add(Company(code="API-001", name="接口测试企业", industry="电力", region="测试"))
    db.commit()
    db.close()
    return TestClient(app)


def test_login_success(client):
    res = client.post("/api/auth/login", json={"username": "admin", "password": "123456"})
    assert res.status_code == 200
    assert res.json()["user"]["role"] == "admin"


def test_login_wrong_password(client):
    res = client.post("/api/auth/login", json={"username": "admin", "password": "wrong-pass"})
    assert res.status_code == 401


def test_me_requires_auth(client):
    client.cookies.clear()
    res = client.get("/api/auth/me")
    assert res.status_code == 401


def test_dashboard_stats(client):
    client.post("/api/auth/login", json={"username": "admin", "password": "123456"})
    res = client.get("/api/dashboard/stats")
    assert res.status_code == 200
    data = res.json()
    assert data["total_companies"] >= 1
    assert "emission_total" in data
    assert "quota_total" in data
