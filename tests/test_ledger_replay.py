"""统一账本重放与对账链路服务层测试。

覆盖：
- 事件实时登记：每笔流水同事务生成事件，事务回滚则事件一同回滚；
- 全量重放投影 == 账户实际余额（持仓/冻结/占用三栏）；
- 事件链完整性：seq 连续、prev_hash/chain_hash 勾连、篡改可检出；
- 对账：分配/交易/冻结/清缴/订单/竞价/冲正/违约全链路 balanced、系统守恒；
- 旧记录回填：清空事件链后从五类业务表幂等补登，重放与对账结论不变；
- 跨年度：不同年度账户独立投影，事件强制带年度；
- 并发：多线程订单交割/竞价结算后事件链不重号不断链，重放仍账实相符；
- 注入差异：篡改余额/物理删除事件/重复退还时对账必须报错并给出 code。
"""

from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import create_engine, event, func
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
from app.core.event_hooks import install_ledger_hooks
from app.models import (
    AllowanceAccount,
    AllowanceTransaction,
    AuctionTrade,
    Company,
    ComplianceRecord,
    LedgerCheckpoint,
    LedgerEvent,
    LedgerReconciliation,
)
from app.services.auction_service import (
    Operator,
    create_session,
    open_session,
    place_bid,
    reverse_settled_trades,
    run_matching,
    settle_session,
)
from app.services.ledger_event_service import backfill_ledger_events
from app.services.quota_service import allocate_quota
from app.services.reconciliation_service import run_reconciliation, serialize_run
from app.services.replay_service import (
    rebuild_checkpoints,
    replay_account,
    replay_all,
    replay_events_timeline,
)
from app.services.trade_order_service import (
    confirm_order,
    create_order,
    deliver_order,
)

ADMIN = Operator(id=1, username="admin", role="admin")


def approx(value, rel=1e-6):
    return pytest.approx(float(value), rel=rel)


def _make_engine(tmp_path=None, mem=False):
    if mem:
        engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    else:
        engine = create_engine(
            f"sqlite:///{tmp_path / 'ledger.db'}",
            connect_args={"check_same_thread": False, "timeout": 30},
        )

        @event.listens_for(engine, "connect")
        def _busy(dbapi_conn, _rec):
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA busy_timeout=30000")
            cur.close()

    Base.metadata.create_all(engine)
    return engine


def _session_factory(engine):
    factory = sessionmaker(bind=engine, autoflush=False)
    install_ledger_hooks(factory)
    return factory


@pytest.fixture()
def db():
    engine = _make_engine(mem=True)
    factory = _session_factory(engine)
    session = factory()
    yield session
    session.close()


@pytest.fixture()
def fdb(tmp_path):
    """文件型库（多线程共享）。"""
    engine = _make_engine(tmp_path=tmp_path)
    factory = _session_factory(engine)
    session = factory()
    yield session, factory
    session.close()
    engine.dispose()


def _company(db, code, name):
    c = Company(code=code, name=name, industry="电力", region="华东")
    db.add(c)
    db.flush()
    return c


def _recon(db, key="k"):
    return serialize_run(run_reconciliation(db, idempotency_key=key))


# --------------------------------------------------------------------------- #
# 事件实时登记
# --------------------------------------------------------------------------- #

class TestEventRecording:
    def test_allocation_records_event_in_same_transaction(self, db):
        c = _company(db, "C1", "甲")
        allocate_quota(db, c.id, 2025, 100, 1000)
        db.commit()
        events = db.query(LedgerEvent).all()
        assert len(events) == 1
        ev = events[0]
        assert ev.seq == 1
        assert ev.source == "quota_allocation"
        assert ev.event_type == "allocation"
        assert ev.direction == "credit"
        assert float(ev.amount) == 1000
        assert ev.year == 2025
        assert ev.is_legacy == 0
        assert ev.chain_hash
        assert ev.prev_hash == ""

    def test_event_rolled_back_with_business_transaction(self, db):
        c = _company(db, "C1", "甲")
        db.commit()
        # 分配一个负净额配额会在事务内抛错并整体回滚：事件不能残留
        with pytest.raises(Exception):
            allocate_quota(db, c.id, 2025, 100, 100, adjustment=-300)
        db.rollback()
        assert db.query(LedgerEvent).count() == 0
        assert db.query(AllowanceTransaction).count() == 0

    def test_every_flow_has_event_and_chain_is_contiguous(self, db):
        c1 = _company(db, "C1", "甲")
        c2 = _company(db, "C2", "乙")
        allocate_quota(db, c1.id, 2025, 0, 1000)
        allocate_quota(db, c2.id, 2025, 0, 1000)
        db.commit()
        o = create_order(db, c1.id, c2.id, 2025, amount=100, price=10, initiator="seller")
        confirm_order(db, o.id, c2.id)
        deliver_order(db, o.id, c1.id)
        db.commit()
        tx_count = db.query(AllowanceTransaction).count()
        ev_count = db.query(LedgerEvent).filter(LedgerEvent.direction != "status").count()
        # 2 分配 + 1 占用 + 1 出库 + 1 到账 = 5 流水 = 5 余额事件
        assert tx_count == 5
        assert ev_count == 5
        seqs = [x[0] for x in db.query(LedgerEvent.seq).order_by(LedgerEvent.seq).all()]
        assert seqs == list(range(1, len(seqs) + 1))

    def test_concurrent_deliveries_keep_chain_consistent(self, fdb):
        db, factory = fdb
        sellers = []
        for i in range(4):
            c = Company(code=f"S{i}", name=f"卖方{i}")
            db.add(c)
            sellers.append(c)
        buyer = Company(code="B", name="买方")
        db.add(buyer)
        db.flush()
        for c in sellers + [buyer]:
            allocate_quota(db, c.id, 2025, 0, 1000)
        db.commit()
        seller_ids = [c.id for c in sellers]
        buyer_id = buyer.id

        def worker(idx):
            s = factory()
            try:
                o = create_order(s, seller_ids[idx], buyer_id, 2025, 10, 10,
                                 initiator="seller")
                confirm_order(s, o.id, buyer_id)
                deliver_order(s, o.id, seller_ids[idx])
                s.commit()
            finally:
                s.close()

        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(worker, range(4)))

        seqs = [x[0] for x in db.query(LedgerEvent.seq).order_by(LedgerEvent.seq).all()]
        assert seqs == list(range(1, len(seqs) + 1))
        result = _recon(db, "concurrent")
        assert result["status"] == "balanced", result["discrepancies"]
        # 买方收到 4*10
        buyer_acc = db.query(AllowanceAccount).filter_by(
            company_id=buyer_id, year=2025
        ).one()
        assert float(buyer_acc.current_balance) == approx(1040)


# --------------------------------------------------------------------------- #
# 重放
# --------------------------------------------------------------------------- #

class TestReplay:
    def test_replay_matches_actual_balances(self, db):
        c1 = _company(db, "C1", "甲")
        c2 = _company(db, "C2", "乙")
        allocate_quota(db, c1.id, 2025, 0, 1000)
        allocate_quota(db, c2.id, 2025, 0, 1000)
        db.commit()
        o = create_order(db, c1.id, c2.id, 2025, amount=300, price=10, initiator="seller")
        confirm_order(db, o.id, c2.id)
        db.commit()
        # 确认后卖方占用 300
        seller_acc = db.query(AllowanceAccount).filter_by(
            company_id=c1.id, year=2025
        ).one()
        st = replay_account(db, seller_acc.id)
        assert st.reserved == approx(300)
        assert st.current == approx(1000)

        deliver_order(db, o.id, c1.id)
        db.commit()
        st = replay_account(db, seller_acc.id)
        assert st.current == approx(700)
        assert st.reserved == approx(0)

    def test_replay_all_and_checkpoint_roundtrip(self, db):
        c1 = _company(db, "C1", "甲")
        c2 = _company(db, "C2", "乙")
        allocate_quota(db, c1.id, 2025, 0, 500)
        allocate_quota(db, c2.id, 2025, 0, 800)
        db.commit()
        states = replay_all(db)
        rebuild_checkpoints(db)
        cps = {cp.account_id: cp for cp in db.query(LedgerCheckpoint).all()}
        assert len(cps) == 2
        for acc_id, st in states.items():
            assert float(cps[acc_id].current_balance) == approx(st.current)
            assert cps[acc_id].last_seq == st.last_seq

    def test_timeline_exposes_running_balances(self, db):
        c = _company(db, "C1", "甲")
        allocate_quota(db, c.id, 2025, 0, 500)
        db.commit()
        items, has_more = replay_events_timeline(db, company_id=c.id)
        assert not has_more
        assert items[-1]["balance_after"] == approx(500)
        assert items[-1]["domain"] == "quota"

    def test_cross_year_accounts_replay_independently(self, db):
        c = _company(db, "C1", "甲")
        allocate_quota(db, c.id, 2025, 0, 500)
        allocate_quota(db, c.id, 2026, 0, 900)
        db.commit()
        states = replay_all(db, company_id=c.id)
        acc25 = db.query(AllowanceAccount).filter_by(company_id=c.id, year=2025).one()
        acc26 = db.query(AllowanceAccount).filter_by(company_id=c.id, year=2026).one()
        assert states[acc25.id].current == approx(500)
        assert states[acc26.id].current == approx(900)
        # 余额事件必须全部带年度
        assert db.query(LedgerEvent).filter(
            LedgerEvent.direction != "status", LedgerEvent.year.is_(None)
        ).count() == 0


# --------------------------------------------------------------------------- #
# 事件时间线分页（before_seq 游标）
# --------------------------------------------------------------------------- #

class TestTimelinePagination:
    """before_seq 翻页：快照必须承接页前真实投影，不从零重算。"""

    def _seed_events(self, db):
        """两企业 2025 年度各 1000 配额 + 3 笔订单交割（共 11 条余额事件）。"""
        c1 = _company(db, "C1", "甲")
        c2 = _company(db, "C2", "乙")
        allocate_quota(db, c1.id, 2025, 0, 1000)
        allocate_quota(db, c2.id, 2025, 0, 1000)
        db.commit()
        for _ in range(3):
            o = create_order(db, c1.id, c2.id, 2025, amount=100, price=10,
                             initiator="seller")
            confirm_order(db, o.id, c2.id)
            deliver_order(db, o.id, c1.id)
            db.commit()
        return c1, c2

    @staticmethod
    def _paged(db, limit, **filters):
        """用 before_seq 逐页向前翻，拼出完整升序时间线。"""
        collected: list[dict] = []
        cursor = None
        while True:
            items, has_more = replay_events_timeline(
                db, limit=limit, before_seq=cursor, **filters
            )
            collected = items + collected
            if not has_more:
                break
            cursor = items[0]["seq"]
        return collected

    def test_paged_timeline_matches_full_replay(self, db):
        self._seed_events(db)
        full, has_more = replay_events_timeline(db, limit=500)
        assert not has_more
        assert len(full) > 5  # 保证小页确实跨页
        for limit in (1, 2, 3, 4, 5):
            paged = self._paged(db, limit)
            # 页间不重叠、不遗漏，且逐条快照与一次性全量一致
            assert [i["seq"] for i in paged] == [i["seq"] for i in full]
            for got, want in zip(paged, full):
                assert got["balance_after"] == want["balance_after"]
                assert got["frozen_after"] == want["frozen_after"]
                assert got["reserved_after"] == want["reserved_after"]

    def test_tail_page_balances_match_actual_accounts(self, db):
        c1, c2 = self._seed_events(db)
        # 链尾一页（不覆盖链首）：快照必须来自页前累计而非从零
        page, _ = replay_events_timeline(db, limit=3)
        assert len(page) == 3
        last_by_acc = {}
        for item in page:
            if item["account_id"] is not None:
                last_by_acc[item["account_id"]] = item
        for c in (c1, c2):
            acc = db.query(AllowanceAccount).filter_by(
                company_id=c.id, year=2025
            ).one()
            snap = last_by_acc[acc.id]
            assert snap["balance_after"] == approx(float(acc.current_balance))
            assert snap["reserved_after"] == approx(float(acc.reserved_balance))
            assert snap["frozen_after"] == approx(float(acc.frozen_balance))

    def test_filtered_pagination_consistency(self, db):
        c1, c2 = self._seed_events(db)
        allocate_quota(db, c1.id, 2026, 0, 900)
        db.commit()
        # 企业筛选：翻页拼接 == 同口径一次性全量
        full_c1, _ = replay_events_timeline(db, company_id=c1.id, limit=500)
        paged_c1 = self._paged(db, 2, company_id=c1.id)
        assert [i["seq"] for i in paged_c1] == [i["seq"] for i in full_c1]
        for got, want in zip(paged_c1, full_c1):
            assert got["balance_after"] == want["balance_after"]
        # 年度筛选：2025 账户快照不受 2026 事件影响，且与真实账户一致
        full_2025, _ = replay_events_timeline(db, year=2025, limit=500)
        paged_2025 = self._paged(db, 2, year=2025)
        assert [i["seq"] for i in paged_2025] == [i["seq"] for i in full_2025]
        acc25 = db.query(AllowanceAccount).filter_by(company_id=c1.id, year=2025).one()
        last = [i for i in paged_2025 if i["account_id"] == acc25.id][-1]
        assert last["balance_after"] == approx(float(acc25.current_balance))

    def test_pagination_stable_when_events_arrive_between_pages(self, db):
        c1, _ = self._seed_events(db)
        page1, has_more = replay_events_timeline(db, limit=4)
        assert has_more
        cursor = page1[0]["seq"]
        # 翻页间隙并发新增事件（append-only，seq 更大）
        allocate_quota(db, c1.id, 2026, 0, 500)
        db.commit()
        new_seq = db.query(func.max(LedgerEvent.seq)).scalar()
        # 继续向前翻完剩余页
        rest: list[dict] = []
        cur = cursor
        while True:
            items, more = replay_events_timeline(db, limit=4, before_seq=cur)
            rest = items + rest
            if not more:
                break
            cur = items[0]["seq"]
        # 已翻页不含新事件；拼接结果与“新事件发生前”的链完全一致
        assert all(i["seq"] != new_seq for i in rest + page1)
        before, _ = replay_events_timeline(db, limit=500, before_seq=new_seq)
        assert [i["seq"] for i in rest + page1] == [i["seq"] for i in before]
        for got, want in zip(rest + page1, before):
            assert got["balance_after"] == want["balance_after"]

    def test_has_more_false_on_exact_multiple_and_empty_page(self, db):
        self._seed_events(db)
        total = db.query(LedgerEvent).count()
        items, has_more = replay_events_timeline(db, limit=total)
        assert len(items) == total
        assert has_more is False
        # 游标指到链首之前：空页且不再继续
        items, has_more = replay_events_timeline(db, before_seq=1)
        assert items == []
        assert has_more is False


# --------------------------------------------------------------------------- #
# 对账
# --------------------------------------------------------------------------- #

class TestReconciliation:
    def test_clean_system_is_balanced_and_conserved(self, db):
        c1 = _company(db, "C1", "甲")
        c2 = _company(db, "C2", "乙")
        allocate_quota(db, c1.id, 2025, 0, 1000)
        allocate_quota(db, c2.id, 2025, 0, 1000)
        db.commit()
        r = _recon(db)
        assert r["status"] == "balanced"
        assert r["conserved"] is True
        assert r["discrepancy_count"] == 0

    def test_idempotent_reconciliation_returns_same_run(self, db):
        c = _company(db, "C1", "甲")
        allocate_quota(db, c.id, 2025, 0, 100)
        db.commit()
        r1 = run_reconciliation(db, idempotency_key="dup")
        r2 = run_reconciliation(db, idempotency_key="dup")
        assert r1.id == r2.id

    def test_order_and_auction_full_chain_balanced(self, db):
        c1 = _company(db, "C1", "甲")
        c2 = _company(db, "C2", "乙")
        allocate_quota(db, c1.id, 2025, 0, 1000)
        allocate_quota(db, c2.id, 2025, 0, 1000)
        db.commit()
        o = create_order(db, c1.id, c2.id, 2025, amount=200, price=10, initiator="seller")
        confirm_order(db, o.id, c2.id)
        deliver_order(db, o.id, c1.id)
        db.commit()
        s = create_session(db, year=2025, name="场次", reserve_price=1, operator=ADMIN)
        open_session(db, s.id, ADMIN)
        place_bid(db, s.id, c1.id, "sell", 100, 50, operator=ADMIN)
        place_bid(db, s.id, c2.id, "buy", 100, 60, operator=ADMIN)
        run_matching(db, s.id, ADMIN)
        settle_session(db, s.id, ADMIN)
        db.commit()
        batch = reverse_settled_trades(db, s.id, ADMIN, "监管冲正测试")
        db.commit()
        assert float(batch.reverse_volume) == 100
        r = _recon(db, "chain")
        assert r["status"] == "balanced", [(d["code"], d["message"]) for d in r["discrepancies"]]
        assert r["conserved"] is True

    def test_tampered_balance_is_detected(self, db):
        c = _company(db, "C1", "甲")
        allocate_quota(db, c.id, 2025, 0, 1000)
        db.commit()
        acc = db.query(AllowanceAccount).filter_by(company_id=c.id, year=2025).one()
        # 库外篡改：凭空增加 500 吨持仓（无事件、无流水）
        acc.current_balance = 1500
        db.commit()
        r = _recon(db, "tamper")
        codes = {d["code"] for d in r["discrepancies"]}
        assert "PROJECTION_CURRENT_MISMATCH" in codes
        assert "FLOW_OPENING_PLUS_TX_MISMATCH" in codes
        assert r["status"] == "discrepancy"

    def test_physically_deleted_event_breaks_chain(self, db):
        c1 = _company(db, "C1", "甲")
        c2 = _company(db, "C2", "乙")
        allocate_quota(db, c1.id, 2025, 0, 100)
        allocate_quota(db, c2.id, 2025, 0, 100)
        db.commit()
        # 物理删除第一条事件：断链与断序都应被检出
        first = db.query(LedgerEvent).order_by(LedgerEvent.seq.asc()).first()
        db.delete(first)
        db.commit()
        r = _recon(db, "del")
        codes = {d["code"] for d in r["discrepancies"]}
        assert "CHAIN_SEQ_GAP" in codes or "CHAIN_PREV_HASH" in codes

    def test_event_content_tampering_is_detected(self, db):
        c = _company(db, "C1", "甲")
        allocate_quota(db, c.id, 2025, 0, 100)
        db.commit()
        ev = db.query(LedgerEvent).one()
        # 直接改库：篡改金额但不重算哈希
        from sqlalchemy import text

        db.execute(text("UPDATE ledger_events SET amount = 999 WHERE id = :id"), {"id": ev.id})
        db.commit()
        r = _recon(db, "hash")
        codes = {d["code"] for d in r["discrepancies"]}
        assert "CHAIN_CONTENT_TAMPERED" in codes


# --------------------------------------------------------------------------- #
# 旧记录回填
# --------------------------------------------------------------------------- #

class TestBackfill:
    def test_backfill_reconstructs_chain_and_conclusion(self, db):
        c1 = _company(db, "C1", "甲")
        c2 = _company(db, "C2", "乙")
        allocate_quota(db, c1.id, 2025, 0, 1000)
        allocate_quota(db, c2.id, 2025, 0, 1000)
        db.commit()
        o = create_order(db, c1.id, c2.id, 2025, amount=150, price=10, initiator="seller")
        confirm_order(db, o.id, c2.id)
        deliver_order(db, o.id, c1.id)
        db.commit()

        # 回填前的期望结论
        before = _recon(db, "before")
        assert before["status"] == "balanced"

        # 模拟未升级旧库：清空事件链与检查点（业务表原样保留）
        db.query(LedgerEvent).delete()
        db.query(LedgerCheckpoint).delete()
        db.commit()
        assert db.query(LedgerEvent).count() == 0

        stats = backfill_ledger_events(db)
        assert stats["tx_events"] == db.query(AllowanceTransaction).count()
        assert stats["status_events"] >= 2  # 订单 confirmed/delivered
        db.commit()

        # 重放余额仍与实际一致
        states = replay_all(db)
        for acc_id, st in states.items():
            acc = db.get(AllowanceAccount, acc_id)
            assert st.current == approx(float(acc.current_balance))
            assert st.reserved == approx(float(acc.reserved_balance))

        # 链连续且全部标记为旧记录
        seqs = [x[0] for x in db.query(LedgerEvent.seq).order_by(LedgerEvent.seq).all()]
        assert seqs == list(range(1, len(seqs) + 1))
        assert db.query(LedgerEvent).filter(LedgerEvent.is_legacy == 0).count() == 0

        after = _recon(db, "after")
        assert after["status"] == before["status"]
        assert after["conserved"] == before["conserved"]

    def test_backfill_is_idempotent(self, db):
        c = _company(db, "C1", "甲")
        allocate_quota(db, c.id, 2025, 0, 100)
        db.commit()
        db.query(LedgerEvent).delete()
        db.commit()
        first = backfill_ledger_events(db)
        second = backfill_ledger_events(db)
        assert first["total"] > 0
        assert second["total"] == 0

    def test_live_recording_after_backfill_does_not_duplicate(self, db):
        c = _company(db, "C1", "甲")
        allocate_quota(db, c.id, 2025, 0, 100)
        db.commit()
        # 库已实时记账，再跑回填不应补任何流水事件
        stats = backfill_ledger_events(db)
        assert stats["tx_events"] == 0
        # 随后新增业务，实时事件继续登记且接在链尾
        allocate_quota(db, c.id, 2026, 0, 200)
        db.commit()
        assert db.query(LedgerEvent).count() == 2
        tail = db.query(LedgerEvent).order_by(LedgerEvent.seq.desc()).first()
        assert tail.year == 2026 and tail.is_legacy == 0


# --------------------------------------------------------------------------- #
# 冲正/违约链路对账
# --------------------------------------------------------------------------- #

class TestReversalAndDefaultReconciliation:
    def _settle(self, db, seller, buyer, qty):
        s = create_session(db, year=2025, name="s", reserve_price=1, operator=ADMIN)
        open_session(db, s.id, ADMIN)
        place_bid(db, s.id, seller.id, "sell", qty, 50, operator=ADMIN)
        place_bid(db, s.id, buyer.id, "buy", qty, 60, operator=ADMIN)
        run_matching(db, s.id, ADMIN)
        settle_session(db, s.id, ADMIN)
        db.commit()
        return s

    def test_partial_reversal_keeps_books_balanced(self, db):
        seller = _company(db, "S", "卖方")
        buyer = _company(db, "B", "买方")
        allocate_quota(db, seller.id, 2025, 0, 1000)
        allocate_quota(db, buyer.id, 2025, 0, 1000)
        db.commit()
        s = self._settle(db, seller, buyer, 100)
        trade = db.query(AuctionTrade).filter_by(session_id=s.id).one()
        # 部分冲正 40 吨
        reverse_settled_trades(db, s.id, ADMIN, "部分冲正",
                               quantities={trade.id: 40})
        db.commit()
        db.refresh(trade)
        assert float(trade.reversed_quantity) == approx(40)
        assert trade.status == "settled"  # 未整笔冲正，保持 settled
        r = _recon(db, "partial")
        assert r["status"] == "balanced", [d["code"] for d in r["discrepancies"]]

        # 再冲正剩余 60 吨 -> reversed
        reverse_settled_trades(db, s.id, ADMIN, "剩余冲正")
        db.commit()
        r = _recon(db, "partial2")
        assert r["status"] == "balanced"

    def test_default_then_auto_recover_balanced(self, db):
        seller = _company(db, "S", "卖方")
        buyer = _company(db, "B", "买方")
        allocate_quota(db, seller.id, 2025, 0, 2000)
        allocate_quota(db, buyer.id, 2025, 0, 0)
        db.commit()
        s = self._settle(db, seller, buyer, 100)
        # 买方把到账配额卖掉 80 吨，仅留 20
        from app.services.trading_service import transfer

        acc = db.query(AllowanceAccount).filter_by(company_id=buyer.id, year=2025).one()
        transfer(db, acc, 80, "sell", counterparty="市场")
        db.commit()
        batch = reverse_settled_trades(db, s.id, ADMIN, "买方不足")
        db.commit()
        assert float(batch.recovered_volume) == approx(20)
        assert float(batch.default_volume) == approx(80)

        # 第二场结算 80 吨到账，自动追偿
        s2 = create_session(db, year=2025, name="s2", reserve_price=1, operator=ADMIN)
        open_session(db, s2.id, ADMIN)
        place_bid(db, s2.id, seller.id, "sell", 80, 50, operator=ADMIN)
        place_bid(db, s2.id, buyer.id, "buy", 80, 60, operator=ADMIN)
        run_matching(db, s2.id, ADMIN)
        settle_session(db, s2.id, ADMIN)
        db.commit()

        trade = db.query(AuctionTrade).filter_by(session_id=s.id).one()
        assert trade.status == "reversed"
        r = _recon(db, "default")
        assert r["status"] == "balanced", [(d["code"], d["message"]) for d in r["discrepancies"]]
        assert r["conserved"] is True


class TestComplianceReconciliation:
    def _approve_report(self, db, company, emission, verifier_id=1):
        from app.models import ActivityData, CalculationMethod, EmissionFactor, EmissionScope
        from app.services.calculation_service import recalc_company_year
        from app.services.mrv_service import approve_report, generate_report, submit_report

        scope = EmissionScope(company_id=company.id, scope="2", category="外购电力", name="用电")
        db.add(scope)
        db.flush()
        db.add(CalculationMethod(method_code="E", name="电", scope="2",
                                 formula_type="activity_factor"))
        db.add(EmissionFactor(factor_code="F", name="电", scope="2", unit="x",
                              value=2.0, valid_from="2024-01-01"))
        db.flush()
        db.add(ActivityData(
            company_id=company.id, scope_id=scope.id, year=2025, period="y",
            activity_type="电", unit="MWh", quantity=emission / 2,
            data_source="x", recorded_by=verifier_id, verified=1,
        ))
        db.commit()
        recalc_company_year(db, company.id, 2025)
        report = generate_report(db, company.id, 2025)
        submit_report(db, report)
        approve_report(db, report, verifier_id)
        db.commit()
        return report

    def test_approved_report_and_clearance_balanced(self, db):
        c = _company(db, "C", "企业")
        allocate_quota(db, c.id, 2025, 0, 1000)
        db.commit()
        self._approve_report(db, c, 800)
        record = db.query(ComplianceRecord).filter_by(company_id=c.id, year=2025, is_active=1).one()
        assert float(record.frozen_amount) == approx(800)
        r = _recon(db, "comp")
        assert r["status"] == "balanced", [d["code"] for d in r["discrepancies"]]

    def test_archived_compliance_after_report_reversal_balanced(self, db):
        from app.services.mrv_service import reverse_report

        c = _company(db, "C", "企业")
        allocate_quota(db, c.id, 2025, 0, 1000)
        db.commit()
        report = self._approve_report(db, c, 800)
        reverse_report(db, report, operator_id=1, reason="报告冲正测试")
        db.commit()
        r = _recon(db, "rev")
        assert r["status"] == "balanced", [d["code"] for d in r["discrepancies"]]
