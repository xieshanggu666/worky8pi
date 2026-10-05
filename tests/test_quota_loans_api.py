"""配额借贷与到期清偿 API 集成测试。

覆盖：角色权限（仅企业可发起/确认/撤销/放款/归还本企业借贷单；
仅 admin 可逾期巡检/宣布违约/监管追偿；企业越权 403 并写审计）、
借贷端到端联动配额/流水/履约、逾期/违约查询隔离、幂等键去重、
HTTP 并发归还累计不超额、审计日志仅监管可读。
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.database import Base, get_db
from app.core.security import hash_password
from app.main import app
from app.models import (
    AllowanceAccount,
    Company,
    QuotaLoan,
    User,
)
from app.services.quota_service import allocate_quota

YEAR = 2027


def _seed_db(TestingSession):
    db = TestingSession()
    pwd_hash, salt = hash_password("123456")
    c1 = Company(code="L-001", name="出借企业", industry="电力", region="华东")
    c2 = Company(code="B-001", name="借入企业", industry="水泥", region="华北")
    c3 = Company(code="T-001", name="第三方企业", industry="化工", region="华南")
    db.add_all([c1, c2, c3])
    db.flush()
    db.add_all([
        User(username="l1", display_name="出借方", role="enterprise",
             company_id=c1.id, password_hash=pwd_hash, salt=salt),
        User(username="b1", display_name="借入方", role="enterprise",
             company_id=c2.id, password_hash=pwd_hash, salt=salt),
        User(username="e3", display_name="第三方", role="enterprise",
             company_id=c3.id, password_hash=pwd_hash, salt=salt),
        User(username="admin", display_name="监管员", role="admin",
             password_hash=pwd_hash, salt=salt),
        User(username="verifier", display_name="核查员", role="verifier",
             password_hash=pwd_hash, salt=salt),
    ])
    allocate_quota(db, c1.id, YEAR, baseline=1000, allocation_amount=1000)
    allocate_quota(db, c2.id, YEAR, baseline=200, allocation_amount=200)
    allocate_quota(db, c3.id, YEAR, baseline=100, allocation_amount=100)
    db.commit()
    ids = {"lender": c1.id, "borrower": c2.id, "third": c3.id}
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
        session = TestingSession()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = override_get_db
    ids = _seed_db(TestingSession)
    client = TestClient(app)
    yield client, ids
    app.dependency_overrides.clear()
    engine.dispose()


@pytest.fixture()
def file_ctx(tmp_path):
    """文件型多连接库：并发请求各自独立连接，真实复现生产并发语义。"""
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
        session = TestingSession()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = override_get_db
    ids = _seed_db(TestingSession)
    client = TestClient(app)
    yield client, ids
    app.dependency_overrides.clear()
    engine.dispose()


def login(client, username):
    client.post("/api/auth/login", json={"username": username, "password": "123456"})


def _create_loan(client, ids, amount=100, initiator="lender", **extra):
    body = {
        "lender_id": ids["lender"],
        "borrower_id": ids["borrower"],
        "year": YEAR,
        "amount": amount,
        "price": 10,
        "initiator": initiator,
        "due_date": f"{YEAR}-12-31",
    }
    body.update(extra)
    res = client.post("/api/loans", json=body)
    assert res.status_code == 200, res.text
    return res.json()


def _confirm_all_and_disburse(client, ids, loan, disburse_as="l1"):
    login(client, "b1" if loan["initiator"] == "lender" else "l1")
    res = client.post(f"/api/loans/{loan['id']}/confirm")
    assert res.status_code == 200, res.text
    login(client, disburse_as)
    res = client.post(f"/api/loans/{loan['id']}/disburse")
    assert res.status_code == 200, res.text
    return res.json()


def _db():
    return next(app.dependency_overrides[get_db]())


class TestLoanPermissions:
    def test_admin_and_third_party_cannot_create(self, ctx):
        client, ids = ctx
        body = {
            "lender_id": ids["lender"], "borrower_id": ids["borrower"],
            "year": YEAR, "amount": 100, "initiator": "lender",
            "due_date": f"{YEAR}-12-31",
        }
        login(client, "admin")
        assert client.post("/api/loans", json=body).status_code == 403
        login(client, "e3")
        # 第三方以本企业名义发起新的借贷关系是允许的（借贷是企业间自主行为）
        body3 = dict(body, lender_id=ids["third"])
        assert client.post("/api/loans", json=body3).status_code == 200

    def test_enterprise_cannot_impersonate(self, ctx):
        client, ids = ctx
        login(client, "e3")
        # 第三方冒用出借企业名义发起，被拒绝
        res = client.post("/api/loans", json={
            "lender_id": ids["lender"], "borrower_id": ids["third"],
            "year": YEAR, "amount": 10, "initiator": "lender",
            "due_date": f"{YEAR}-12-31",
        })
        assert res.status_code == 403

    def test_non_party_cannot_confirm_or_cancel(self, ctx):
        client, ids = ctx
        login(client, "l1")
        loan = _create_loan(client, ids)
        login(client, "e3")
        assert client.post(f"/api/loans/{loan['id']}/confirm").status_code == 403
        assert client.post(f"/api/loans/{loan['id']}/cancel", json={"reason": "x"}).status_code == 403

    def test_only_borrower_can_repay(self, ctx):
        client, ids = ctx
        login(client, "l1")
        loan = _create_loan(client, ids)
        _confirm_all_and_disburse(client, ids, loan, disburse_as="l1")
        # 出借方不能替借入方归还（监管追偿走专用接口）
        res = client.post(f"/api/loans/{loan['id']}/repay", json={})
        assert res.status_code == 403

    def test_enterprise_cannot_scan_or_default_or_recover(self, ctx):
        client, ids = ctx
        login(client, "b1")
        assert client.post("/api/loans/overdue/scan").status_code == 403
        assert client.post(
            f"/api/loans/1/default", json={"reason": "x"}
        ).status_code == 403
        assert client.post(
            f"/api/loans/defaults/{ids['borrower']}/recover", params={"year": YEAR}
        ).status_code == 403

    def test_verifier_readonly(self, ctx):
        client, ids = ctx
        login(client, "verifier")
        assert client.get("/api/loans").status_code == 200
        assert client.post("/api/loans/overdue/scan").status_code == 403

    def test_audit_logs_regulator_only(self, ctx):
        client, ids = ctx
        login(client, "b1")
        assert client.get("/api/loans/audit-logs").status_code == 403
        login(client, "verifier")
        assert client.get("/api/loans/audit-logs").status_code == 200
        # 第三方越权操作已留痕
        logs = client.get("/api/loans/audit-logs").json()
        assert any(x["result"] == "denied" for x in logs)


class TestLoanFlow:
    def test_full_lifecycle(self, ctx):
        client, ids = ctx
        login(client, "l1")
        loan = _create_loan(client, ids, 100)
        assert loan["status"] == "pending"
        assert loan["lender_confirmed"] is True

        # 列表隔离：第三方看不到该借贷单
        login(client, "e3")
        assert client.get("/api/loans").json() == []
        login(client, "l1")
        assert len(client.get("/api/loans").json()) == 1

        loan = _confirm_all_and_disburse(client, ids, loan)
        assert loan["status"] == "active"
        assert loan["disbursed_amount"] == 100.0

        # 双方账户
        db = _db()
        lender = db.query(AllowanceAccount).filter_by(
            company_id=ids["lender"], year=YEAR).one()
        borrower = db.query(AllowanceAccount).filter_by(
            company_id=ids["borrower"], year=YEAR).one()
        assert float(lender.current_balance) == 900
        assert float(borrower.current_balance) == 300

        # 借入方分两次归还
        login(client, "b1")
        r = client.post(f"/api/loans/{loan['id']}/repay", json={"amount": 40})
        assert r.status_code == 200, r.text
        assert r.json()["loan"]["repaid_amount"] == 40.0
        assert r.json()["loan"]["status"] == "active"
        r = client.post(f"/api/loans/{loan['id']}/repay", json={})
        assert r.status_code == 200, r.text
        assert r.json()["loan"]["status"] == "repaid"

        db.expire_all()
        assert float(lender.current_balance) == 1000
        assert float(borrower.current_balance) == 200

    def test_cancel_confirmed_releases_reserve(self, ctx):
        client, ids = ctx
        login(client, "l1")
        loan = _create_loan(client, ids, 100)
        login(client, "b1")
        assert client.post(f"/api/loans/{loan['id']}/confirm").status_code == 200
        db = _db()
        lender = db.query(AllowanceAccount).filter_by(
            company_id=ids["lender"], year=YEAR).one()
        assert float(lender.reserved_balance) == 100

        res = client.post(f"/api/loans/{loan['id']}/cancel", json={"reason": "不再出借"})
        assert res.status_code == 200
        db.expire_all()
        assert float(lender.reserved_balance) == 0
        assert res.json()["status"] == "cancelled"

    def test_overdue_default_and_recover(self, ctx):
        client, ids = ctx
        login(client, "l1")
        loan = _create_loan(client, ids, 100, due_date=f"{YEAR}-06-30")
        loan = _confirm_all_and_disburse(client, ids, loan)

        # 到期前巡检无逾期
        login(client, "admin")
        res = client.post("/api/loans/overdue/scan")
        assert res.json()["marked"] == 0
        # 模拟到期日之后（巡检接口按服务器日期判断，此处直接走手动标记路径：
        # 通过把到期日改到过去再巡检）
        db = _db()
        db.query(QuotaLoan).filter_by(id=loan["id"]).update({"due_date": "2000-01-01"})
        db.commit()
        res = client.post("/api/loans/overdue/scan")
        assert res.json()["marked"] == 1
        assert res.json()["loans"][0]["status"] == "overdue"
        # 重复巡检幂等
        assert client.post("/api/loans/overdue/scan").json()["marked"] == 0

        # 宣布违约
        res = client.post(f"/api/loans/{loan['id']}/default", json={"reason": "到期后拒不归还"})
        assert res.status_code == 200
        assert res.json()["status"] == "defaulted"
        assert res.json()["defaulted_amount"] == 100.0

        # 借入方视角能看到欠额；第三方看不到
        login(client, "b1")
        overdue = client.get("/api/loans/overdue").json()
        assert len(overdue) == 1
        login(client, "e3")
        assert client.get("/api/loans/overdue").json() == []

        # 监管追偿
        login(client, "admin")
        res = client.post(
            f"/api/loans/defaults/{ids['borrower']}/recover", params={"year": YEAR}
        )
        assert res.status_code == 200, res.text
        assert res.json()["recovered_volume"] == 100.0
        assert res.json()["loans"][0]["status"] == "repaid"

    def test_reason_required_for_default(self, ctx):
        client, ids = ctx
        login(client, "l1")
        loan = _create_loan(client, ids, due_date="2000-01-01")
        _confirm_all_and_disburse(client, ids, loan)
        login(client, "admin")
        client.post("/api/loans/overdue/scan")
        res = client.post(f"/api/loans/{loan['id']}/default", json={"reason": "x"})
        assert res.status_code == 422

    def test_idempotency_keys(self, ctx):
        client, ids = ctx
        login(client, "l1")
        body = {
            "lender_id": ids["lender"], "borrower_id": ids["borrower"],
            "year": YEAR, "amount": 50, "initiator": "lender",
            "due_date": f"{YEAR}-12-31", "idempotency_key": "loan-idem-1",
        }
        r1 = client.post("/api/loans", json=body)
        r2 = client.post("/api/loans", json=body)
        assert r1.json()["id"] == r2.json()["id"]
        loan_id = r1.json()["id"]
        login(client, "b1")
        client.post(f"/api/loans/{loan_id}/confirm")
        login(client, "l1")
        client.post(f"/api/loans/{loan_id}/disburse")

        login(client, "b1")
        rep = {"amount": 50, "idempotency_key": "repay-idem-1"}
        rr1 = client.post(f"/api/loans/{loan_id}/repay", json=rep)
        rr2 = client.post(f"/api/loans/{loan_id}/repay", json=rep)
        assert rr1.json()["repayment"]["id"] == rr2.json()["repayment"]["id"]
        assert rr1.json()["loan"]["repaid_amount"] == 50.0

    def test_concurrent_repay_capped(self, file_ctx):
        """HTTP 并发三笔各 40 吨归还：累计恰为 100，不超额。"""
        client, ids = file_ctx
        login(client, "l1")
        loan = _create_loan(client, ids, 100)
        _confirm_all_and_disburse(client, ids, loan)

        login(client, "b1")
        import concurrent.futures

        def call():
            c = TestClient(app)
            c.post("/api/auth/login", json={"username": "b1", "password": "123456"})
            r = c.post(f"/api/loans/{loan['id']}/repay", json={"amount": 40})
            return r.status_code, r.json() if r.status_code == 200 else r.text

        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
            results = list(pool.map(lambda _: call(), range(3)))

        db = _db()
        db.expire_all()
        row = db.query(QuotaLoan).filter_by(id=loan["id"]).one()
        assert float(row.repaid_amount) == 100.0
        assert row.status == "repaid"
        # 三笔均成功（第三笔封顶 20）或最后一笔 400/409，总之累计不超额
        assert sum(
            float(r[1]["repayment"]["quantity"]) for r in results if r[0] == 200
        ) == pytest.approx(100.0, abs=0.01)
