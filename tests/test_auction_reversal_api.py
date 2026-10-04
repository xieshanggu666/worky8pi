"""已结算竞价成交监管冲正与违约回退链路 API 集成测试。

覆盖：角色权限（仅 admin 可冲正/追偿，企业/核查拒绝并审计）、冲正端到端
联动配额/流水/履约、违约欠额查询隔离、手动追偿、幂等键去重、HTTP 并发
冲正只有一次生效。
"""

import pytest
from fastapi.testclient import TestClient

from tests.test_auction_api import (
    YEAR,
    _install_engine,
    _seed_db,
    create_open_session,
    login,
)
from sqlalchemy import create_engine, event
from sqlalchemy.pool import StaticPool

from app.core.database import get_db
from app.main import app
from app.models import (
    AllowanceAccount,
    AllowanceTransaction,
    AuctionTrade,
    ComplianceRecord,
)


@pytest.fixture()
def ctx():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TestingSession = _install_engine(engine)
    ids = _seed_db(TestingSession)
    client = TestClient(app)
    yield client, ids
    app.dependency_overrides.clear()
    engine.dispose()


@pytest.fixture()
def file_ctx(tmp_path):
    """文件型多连接库：并发请求各自独立连接，真实复现生产并发语义。"""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'reverse_api.db'}",
        connect_args={"check_same_thread": False, "timeout": 30},
    )

    @event.listens_for(engine, "connect")
    def _busy_timeout(dbapi_conn, _rec):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA busy_timeout=30000")
        cur.close()

    TestingSession = _install_engine(engine)
    ids = _seed_db(TestingSession)
    client = TestClient(app)
    yield client, ids
    app.dependency_overrides.clear()
    engine.dispose()


def _settle_one(client, ids, sid, qty):
    login(client, "s1")
    client.post(f"/api/auctions/{sid}/bids", json={"side": "sell", "quantity": qty, "price": 80})
    login(client, "b1")
    client.post(f"/api/auctions/{sid}/bids", json={"side": "buy", "quantity": qty, "price": 90})
    login(client, "admin")
    client.post(f"/api/auctions/{sid}/match")
    res = client.post(f"/api/auctions/{sid}/settle")
    assert res.status_code == 200, res.text


def _db():
    return next(app.dependency_overrides[get_db]())


class TestReversalPermissions:
    def test_enterprise_and_verifier_cannot_reverse(self, ctx):
        client, ids = ctx
        sid = create_open_session(client)["id"]
        _settle_one(client, ids, sid, 100)
        body = {"reason": "监管冲正测试"}

        login(client, "b1")
        assert client.post(f"/api/auctions/{sid}/reverse", json=body).status_code == 403
        login(client, "verifier")
        assert client.post(f"/api/auctions/{sid}/reverse", json=body).status_code == 403

        # 冲正记录仅监管/核查可见，企业越权读取 403 并留痕
        login(client, "b1")
        assert client.get("/api/auctions/reversals").status_code == 403
        login(client, "verifier")
        assert client.get("/api/auctions/reversals").status_code == 200
        login(client, "admin")
        assert client.get("/api/auctions/reversals").status_code == 200

    def test_reverse_requires_reason(self, ctx):
        client, ids = ctx
        sid = create_open_session(client)["id"]
        _settle_one(client, ids, sid, 100)
        login(client, "admin")
        res = client.post(f"/api/auctions/{sid}/reverse", json={"reason": "x"})
        assert res.status_code == 422

    def test_cannot_reverse_unsettled(self, ctx):
        client, ids = ctx
        sid = create_open_session(client)["id"]
        login(client, "admin")
        res = client.post(f"/api/auctions/{sid}/reverse", json={"reason": "提前冲正"})
        assert res.status_code == 400
        assert "仅已结算" in res.json()["detail"]


class TestReversalFlow:
    def test_full_reverse_restores_accounts(self, ctx):
        client, ids = ctx
        sid = create_open_session(client)["id"]
        _settle_one(client, ids, sid, 300)

        login(client, "admin")
        res = client.post(f"/api/auctions/{sid}/reverse",
                          json={"reason": "成交异常，监管冲正"})
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["trade_count"] == 1
        assert body["reverse_volume"] == 300
        assert body["recovered_volume"] == 300
        assert body["default_volume"] == 0
        assert len(body["reversals"]) == 1

        acc_s = client.get(f"/api/companies/{ids['c1']}/account?year={YEAR}").json()
        acc_b = client.get(f"/api/companies/{ids['c2']}/account?year={YEAR}").json()
        assert acc_s["current_balance"] == 1000
        assert acc_b["current_balance"] == 200

        # 成交单状态与冲正查询
        trades = client.get("/api/auctions/trades/all").json()
        assert trades[0]["status"] == "reversed"
        assert trades[0]["reversed_quantity"] == 300
        rev = client.get("/api/auctions/reversals").json()
        assert len(rev["batches"]) == 1
        assert rev["reversals"][0]["recovered_quantity"] == 300

    def test_partial_reverse_keeps_settled(self, ctx):
        client, ids = ctx
        sid = create_open_session(client)["id"]
        _settle_one(client, ids, sid, 300)
        login(client, "admin")
        trades = client.get("/api/auctions/trades/all").json()
        tid = trades[0]["id"]

        res = client.post(f"/api/auctions/{sid}/reverse",
                          json={"reason": "部分冲正", "quantities": {str(tid): 100}})
        assert res.status_code == 200
        t = client.get("/api/auctions/trades/all").json()[0]
        assert t["status"] == "settled"
        assert t["reversed_quantity"] == 100

    def test_reverse_idempotent(self, ctx):
        client, ids = ctx
        sid = create_open_session(client)["id"]
        _settle_one(client, ids, sid, 100)
        login(client, "admin")
        headers = {"Idempotency-Key": "rev-api-1"}
        b1 = client.post(f"/api/auctions/{sid}/reverse",
                         json={"reason": "幂等冲正"}, headers=headers).json()
        b2 = client.post(f"/api/auctions/{sid}/reverse",
                         json={"reason": "幂等冲正"}, headers=headers).json()
        assert b1["id"] == b2["id"]
        db = _db()
        assert db.query(AuctionTrade).filter_by(status="reversed").count() == 1
        db.close()

    def test_duplicate_reverse_rejected(self, ctx):
        client, ids = ctx
        sid = create_open_session(client)["id"]
        _settle_one(client, ids, sid, 100)
        login(client, "admin")
        client.post(f"/api/auctions/{sid}/reverse", json={"reason": "第一次冲正"})
        res = client.post(f"/api/auctions/{sid}/reverse", json={"reason": "重复冲正"})
        assert res.status_code == 400

    def test_concurrent_reverse_only_once(self, file_ctx):
        """HTTP 多线程并发冲正：幂等键去重 + 状态抢占，只有一次回退。"""
        from concurrent.futures import ThreadPoolExecutor

        client, ids = file_ctx
        sid = create_open_session(client)["id"]
        _settle_one(client, ids, sid, 300)

        def fire(i):
            c = TestClient(app)
            c.post("/api/auth/login", json={"username": "admin", "password": "123456"})
            r = c.post(f"/api/auctions/{sid}/reverse",
                       json={"reason": f"并发冲正{i}"},
                       headers={"Idempotency-Key": f"rev-c-{i}"})
            return r.status_code

        with ThreadPoolExecutor(max_workers=5) as pool:
            codes = list(pool.map(fire, range(5)))
        assert codes.count(200) == 1
        assert set(codes) <= {200, 400}
        db = _db()
        assert db.query(AllowanceTransaction).filter_by(
            tx_type="auction_clawback_out").count() == 1
        acc = db.query(AllowanceAccount).filter_by(company_id=ids["c2"], year=YEAR).one()
        assert float(acc.current_balance) == 200
        db.close()


class TestDefaultAndRecoveryFlow:
    def _make_default(self, client, ids, sid=1, qty=300, sell_out=450):
        _settle_one(client, ids, sid, qty)
        # 买方到账后转出大部分，制造冲正违约
        db = _db()
        from app.services.trading_service import transfer
        acc = db.query(AllowanceAccount).filter_by(company_id=ids["c2"], year=YEAR).one()
        transfer(db, acc, sell_out, "sell", counterparty="碳市场", tx_date="2026-04-01")
        db.commit()
        db.close()
        login(client, "admin")
        client.post(f"/api/auctions/{sid}/reverse", json={"reason": "违约冲正"})

    def test_default_listing_isolated_and_repay(self, ctx):
        client, ids = ctx
        sid = create_open_session(client)["id"]
        self._make_default(client, ids, sid=sid, qty=300, sell_out=450)
        # 欠 250
        login(client, "admin")
        defaults = client.get(f"/api/auctions/defaults?year={YEAR}").json()
        assert len(defaults) == 1
        assert defaults[0]["default_outstanding"] == 250
        tid = defaults[0]["id"]

        # 企业只看到本企业欠额
        login(client, "s1")
        assert client.get(f"/api/auctions/defaults?year={YEAR}").json() == []
        login(client, "b1")
        mine = client.get(f"/api/auctions/defaults?year={YEAR}").json()
        assert len(mine) == 1 and mine[0]["id"] == tid

        # 企业不能追偿
        assert client.post(f"/api/auctions/trades/{tid}/repay", json={}).status_code == 403

        # 监管追偿：买方无自由可用 → 400
        login(client, "admin")
        res = client.post(f"/api/auctions/trades/{tid}/repay", json={})
        assert res.status_code == 400

        # 买方补入 300 后监管追偿，欠额 250 一次结清
        db = _db()
        from app.services.trading_service import transfer
        acc = db.query(AllowanceAccount).filter_by(company_id=ids["c2"], year=YEAR).one()
        transfer(db, acc, 300, "buy", counterparty="碳市场", tx_date="2026-05-01")
        db.commit()
        db.close()
        res = client.post(f"/api/auctions/trades/{tid}/repay", json={})
        assert res.status_code == 200
        assert res.json()["repayment"]["quantity"] == 250
        assert res.json()["trade"]["status"] == "reversed"

        # 卖方：出库 300 后 700 + 冲正即时收回 50 + 追偿 250 = 1000
        acc_s = client.get(f"/api/companies/{ids['c1']}/account?year={YEAR}").json()
        assert acc_s["current_balance"] == 1000
        assert client.get(f"/api/auctions/defaults?year={YEAR}").json() == []

    def test_buyer_level_recover_endpoint(self, ctx):
        client, ids = ctx
        sid = create_open_session(client)["id"]
        self._make_default(client, ids, sid=sid, qty=300, sell_out=470)
        db = _db()
        from app.services.trading_service import transfer
        acc = db.query(AllowanceAccount).filter_by(company_id=ids["c2"], year=YEAR).one()
        transfer(db, acc, 100, "buy", counterparty="碳市场", tx_date="2026-05-01")
        db.commit()
        db.close()
        login(client, "admin")
        # 欠 270，仅可追偿 100
        res = client.post(
            f"/api/auctions/defaults/{ids['c2']}/recover?year={YEAR}")
        assert res.status_code == 200
        assert res.json()["recovered_volume"] == 100
        defaults = client.get(f"/api/auctions/defaults?year={YEAR}").json()
        assert defaults[0]["default_outstanding"] == 170

    def test_reverse_writes_audit(self, ctx):
        client, ids = ctx
        sid = create_open_session(client)["id"]
        _settle_one(client, ids, sid, 100)
        login(client, "admin")
        client.post(f"/api/auctions/{sid}/reverse", json={"reason": "审计冲正"})
        logs = client.get("/api/auctions/audit-logs").json()
        actions = {x["action"] for x in logs}
        assert "trade.reverse" in actions
        assert "session.reverse" in actions
