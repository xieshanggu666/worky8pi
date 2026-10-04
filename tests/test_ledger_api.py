"""统一账本重放/对账 API 集成测试。

覆盖：
- 权限：对账/回填/重建检查点仅监管；企业只能读本企业事件链与对账结论；
- 事件链查询：游标分页、运行余额、链头信息；
- 单账户重放：重放投影与实际余额；
- 对账触发：balanced 结论、Idempotency-Key 去重、差异结构化返回；
- 回填：admin 可把旧记录补登为历史事件，重复回填幂等；
- 全链路：经订单/竞价真实 HTTP 流程后对账仍守恒。
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.database import Base, get_db
from app.core.event_hooks import install_ledger_hooks
from app.core.security import hash_password
from app.main import app
from app.models import (
    AllowanceAccount,
    Company,
    LedgerEvent,
    User,
)
from app.services.quota_service import allocate_quota
from app.services.trade_order_service import (
    confirm_order,
    create_order,
    deliver_order,
)


def _install_engine(engine):
    TestingSession = sessionmaker(bind=engine, autoflush=False)
    Base.metadata.create_all(engine)
    install_ledger_hooks(TestingSession)

    def override_get_db():
        session = TestingSession()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = override_get_db
    return TestingSession


def _seed(TestingSession):
    db = TestingSession()
    pwd, salt = hash_password("123456")
    c1 = Company(code="A", name="甲企业", industry="电力", region="华东")
    c2 = Company(code="B", name="乙企业", industry="水泥", region="华北")
    db.add_all([c1, c2])
    db.flush()
    db.add_all([
        User(username="s1", display_name="卖方", role="enterprise",
             company_id=c1.id, password_hash=pwd, salt=salt),
        User(username="b1", display_name="买方", role="enterprise",
             company_id=c2.id, password_hash=pwd, salt=salt),
        User(username="admin", display_name="监管", role="admin",
             password_hash=pwd, salt=salt),
        User(username="verifier", display_name="核查", role="verifier",
             password_hash=pwd, salt=salt),
    ])
    allocate_quota(db, c1.id, 2026, 1000, 1000)
    allocate_quota(db, c2.id, 2026, 1000, 1000)
    db.commit()
    ids = {"c1": c1.id, "c2": c2.id}
    db.close()
    return ids


@pytest.fixture()
def ctx():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TestingSession = _install_engine(engine)
    ids = _seed(TestingSession)
    client = TestClient(app)
    yield client, ids, TestingSession
    app.dependency_overrides.clear()
    engine.dispose()


def login(client, username):
    res = client.post("/api/auth/login", json={"username": username, "password": "123456"})
    assert res.status_code == 200


def _account_id(TestingSession, company_id, year=2026):
    db = TestingSession()
    try:
        return db.query(AllowanceAccount).filter_by(company_id=company_id, year=year).one().id
    finally:
        db.close()


class TestLedgerPermissions:
    def test_enterprise_cannot_reconcile(self, ctx):
        client, _, _ = ctx
        login(client, "s1")
        res = client.post("/api/ledger/reconcile")
        assert res.status_code == 403

    def test_enterprise_cannot_backfill(self, ctx):
        client, _, _ = ctx
        login(client, "s1")
        assert client.post("/api/ledger/backfill").status_code == 403
        login(client, "verifier")
        assert client.post("/api/ledger/backfill").status_code == 403

    def test_anonymous_rejected(self, ctx):
        client, _, _ = ctx
        assert client.get("/api/ledger/events").status_code == 401
        assert client.post("/api/ledger/reconcile").status_code == 401

    def test_verifier_can_reconcile_but_not_backfill(self, ctx):
        client, _, _ = ctx
        login(client, "verifier")
        res = client.post("/api/ledger/reconcile")
        assert res.status_code == 200
        assert res.json()["status"] == "balanced"

    def test_enterprise_event_scope_isolation(self, ctx):
        client, ids, _ = ctx
        login(client, "s1")
        res = client.get("/api/ledger/events")
        assert res.status_code == 200
        items = res.json()["items"]
        assert items
        # 企业只能看到本企业事件
        assert all(item["company_id"] == ids["c1"] for item in items)
        # 越权指定其他企业
        assert client.get(f"/api/ledger/events?company_id={ids['c2']}").status_code == 403
        # 越权重放他企业账户
        other_acc = _account_id(ctx[2], ids["c2"])
        assert client.get(f"/api/ledger/accounts/{other_acc}/replay").status_code == 403


class TestLedgerQueries:
    def test_chain_head_and_event_paging(self, ctx):
        client, ids, _ = ctx
        login(client, "admin")
        head = client.get("/api/ledger/chain/head").json()
        assert head["head_seq"] == 2
        assert head["total_events"] == 2
        assert head["live_events"] == 2

        res = client.get("/api/ledger/events?limit=1").json()
        assert len(res["items"]) == 1
        assert res["has_more"] is True
        cursor = res["next_before_seq"]
        page2 = client.get(f"/api/ledger/events?limit=1&before_seq={cursor}").json()
        assert len(page2["items"]) == 1
        assert page2["items"][0]["seq"] < cursor

    def test_event_paging_cursor_no_overlap_and_real_balances(self, ctx):
        client, ids, TS = ctx
        # 追加一笔订单交割：2 分配 + 占用/出库/到账 = 5 条余额事件
        db = TS()
        o = create_order(db, ids["c1"], ids["c2"], 2026, amount=100, price=10,
                         initiator="seller")
        confirm_order(db, o.id, ids["c2"])
        deliver_order(db, o.id, ids["c1"])
        db.commit()
        expected = {
            acc.company_id: float(acc.current_balance)
            for acc in db.query(AllowanceAccount).all()
        }
        db.close()

        login(client, "admin")
        # 一次性大页作为基准（5 条事件、limit=500：不足一页，无游标）
        full = client.get("/api/ledger/events?limit=500").json()
        assert full["has_more"] is False
        assert full["next_before_seq"] is None
        assert len(full["items"]) == 5

        # limit=2 向前翻页：页间不重叠不遗漏，游标锚定每页最小 seq
        collected = []
        cursor = None
        while True:
            url = "/api/ledger/events?limit=2"
            if cursor is not None:
                url += f"&before_seq={cursor}"
            body = client.get(url).json()
            assert body["items"]
            collected = body["items"] + collected
            if not body["has_more"]:
                assert body["next_before_seq"] is None
                break
            cursor = body["next_before_seq"]
            assert cursor == body["items"][0]["seq"]
        assert [i["seq"] for i in collected] == [i["seq"] for i in full["items"]]
        for got, want in zip(collected, full["items"]):
            assert got["balance_after"] == want["balance_after"]

        # 翻页后的快照与真实账户一致：链尾每账户最后一条 == 账户当前持仓
        last_by_acc = {}
        for item in full["items"]:
            if item["account_id"] is not None:
                last_by_acc[item["company_id"]] = item
        for company_id, balance in expected.items():
            assert last_by_acc[company_id]["balance_after"] == balance

        # 中间页（非链首）快照不从零重算：第二页首条承接页前投影
        page2 = client.get("/api/ledger/events?limit=2&before_seq=4").json()
        assert [i["seq"] for i in page2["items"]] == [2, 3]
        assert page2["items"][0]["balance_after"] == 1000  # c2 分配入账后
        # c1 的分配（seq=1）在本页之前：若从零重算这里会是 0
        assert page2["items"][1]["balance_after"] == 1000
        assert page2["items"][1]["reserved_after"] == 100  # c1 订单确认占用

    def test_account_replay(self, ctx):
        client, ids, TS = ctx
        acc = _account_id(TS, ids["c1"])
        login(client, "admin")
        body = client.get(f"/api/ledger/accounts/{acc}/replay").json()
        assert body["replayed"]["current_balance"] == 1000
        assert body["actual"]["current_balance"] == 1000
        assert body["events_applied"] == 1
        assert body["snapshot_mismatch_count"] == 0


class TestReconcileApi:
    def test_reconcile_balanced_and_idempotent(self, ctx):
        client, _, _ = ctx
        login(client, "admin")
        r1 = client.post("/api/ledger/reconcile", headers={"Idempotency-Key": "rc-1"}).json()
        r2 = client.post("/api/ledger/reconcile", headers={"Idempotency-Key": "rc-1"}).json()
        assert r1["recon_no"] == r2["recon_no"]
        assert r1["status"] == "balanced"
        assert r1["conserved"] is True
        assert r1["checked_events"] >= 2

        listed = client.get("/api/ledger/reconciliations").json()
        assert any(r["recon_no"] == r1["recon_no"] for r in listed)
        detail = client.get(f"/api/ledger/reconciliations/{r1['id']}").json()
        assert detail["recon_no"] == r1["recon_no"]

    def test_reconcile_detects_tampering(self, ctx):
        client, ids, TS = ctx
        db = TS()
        acc = db.query(AllowanceAccount).filter_by(company_id=ids["c1"], year=2026).one()
        acc.current_balance = 5000  # 库外虚增
        db.commit()
        db.close()
        login(client, "admin")
        body = client.post("/api/ledger/reconcile").json()
        assert body["status"] == "discrepancy"
        codes = {d["code"] for d in body["discrepancies"]}
        assert "PROJECTION_CURRENT_MISMATCH" in codes

    def test_checkpoint_rebuild_admin_only(self, ctx):
        client, _, _ = ctx
        login(client, "verifier")
        assert client.post("/api/ledger/checkpoints/rebuild").status_code == 403
        login(client, "admin")
        res = client.post("/api/ledger/checkpoints/rebuild")
        assert res.status_code == 200
        assert res.json()["accounts"] == 2


class TestBackfillApi:
    def test_backfill_legacy_records(self, ctx):
        client, ids, TS = ctx
        # 模拟旧库：清空事件链（业务流水仍在）
        db = TS()
        deleted = db.query(LedgerEvent).delete()
        db.commit()
        assert deleted == 2
        db.close()

        login(client, "admin")
        res = client.post("/api/ledger/backfill")
        assert res.status_code == 200
        stats = res.json()["backfilled"]
        assert stats["tx_events"] == 2
        assert stats["status_events"] == 0  # 仅分配，无单据状态

        # 回填事件全部带旧标记且链连续
        db = TS()
        try:
            seqs = sorted(x[0] for x in db.query(LedgerEvent.seq).all())
            assert seqs == list(range(1, len(seqs) + 1))
            assert all(x[0] == 1 for x in db.query(LedgerEvent.is_legacy).all())
        finally:
            db.close()

        # 回填后对账平衡
        body = client.post("/api/ledger/reconcile").json()
        assert body["status"] == "balanced"

        # 再次回填幂等
        again = client.post("/api/ledger/backfill").json()
        assert again["backfilled"]["total"] == 0
