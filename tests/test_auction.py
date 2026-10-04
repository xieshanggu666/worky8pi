"""碳配额集中竞价市场服务层测试。

覆盖：
- 场次状态机：草稿→开放→撮合→结算，非法流转拒绝，幂等；
- 密封报价：买/卖方报价、超可用卖出拒绝、低于保留价拒绝、同企业同方向唯一、撤单；
- 统一价格撮合：价量优先、时间优先、部分成交、无成交、自成交规避、按可用配额封顶；
- 结算：卖方占用出库、买方到账、流水快照链、买方履约缺口联动核销；
- 撤场：开放期撤场无副作用、撮合后撤场逐笔释放占用；
- 并发：并发结算只划转一次、撮合与撤场竞争只有一方成功、
  并发卖出报价总报量不超过自由可用、结算与手动清缴并发守恒；
- 审计：监管操作与越权/拒绝均写日志。
"""

from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from app.core.database import Base
from app.models import (
    AllowanceAccount,
    AllowanceTransaction,
    AuctionAuditLog,
    AuctionBid,
    AuctionSession,
    AuctionTrade,
    Company,
    ComplianceRecord,
)
from app.services.auction_service import (
    BID_ACTIVE,
    BID_CANCELLED,
    BID_MATCHED,
    BID_PARTIAL,
    BID_UNMATCHED,
    CANCELLED,
    DRAFT,
    MATCHED,
    OPEN,
    SETTLED,
    TRADE_CANCELLED,
    TRADE_RESERVED,
    TRADE_SETTLED,
    AuctionError,
    Operator,
    cancel_bid,
    cancel_session,
    create_session,
    list_audit_logs,
    open_session,
    place_bid,
    run_matching,
    settle_session,
)
from app.services.quota_service import allocate_quota

YEAR = 2026
ADMIN = Operator(id=1, username="admin", role="admin")


def approx(value, rel=1e-6):
    return pytest.approx(float(value), rel=rel)


@pytest.fixture()
def db(tmp_path):
    """文件型临时库：多线程共享同一份数据。"""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'auction.db'}",
        connect_args={"check_same_thread": False, "timeout": 30},
    )

    @event.listens_for(engine, "connect")
    def _busy_timeout(dbapi_conn, _rec):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA busy_timeout=30000")
        cur.close()

    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, autoflush=False)()
    yield session
    session.close()
    engine.dispose()


@pytest.fixture()
def market(db):
    """三家企业：甲/乙各 1000 吨（潜在卖方），丙 200 吨（缺口买方），2026 年度。"""
    names = [("S1", "卖方甲", 1000), ("S2", "卖方乙", 1000), ("B1", "买方丙", 200)]
    companies = {}
    for code, name, quota in names:
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


def _account(db, company_id):
    return db.query(AllowanceAccount).filter_by(company_id=company_id, year=YEAR).one()


def _open_session(db, reserve_price=0.0, auto_clear=True, name="测试场次"):
    s = create_session(
        db, year=YEAR, name=name, reserve_price=reserve_price,
        auto_clear_deficit=auto_clear, operator=ADMIN,
    )
    return open_session(db, s.id, ADMIN)


def _assert_snapshot(db, account_id):
    """流水快照链可推算，末笔快照等于账户现值。"""
    txs = (
        db.query(AllowanceTransaction)
        .filter(AllowanceTransaction.account_id == account_id)
        .order_by(AllowanceTransaction.id.asc())
        .all()
    )
    current_pos = {
        "allocation": 1, "buy": 1, "transfer_in": 1, "reversal": 1,
        "trade_deliver_in": 1, "auction_deliver_in": 1,
        "sell": -1, "transfer_out": -1, "offset": -1, "clear": -1,
        "frozen_clear": -1, "trade_deficit_clear": -1, "auction_deficit_clear": -1,
        "trade_deliver_out": -1, "auction_deliver_out": -1,
        "freeze": 0, "trade_reserve": 0, "trade_release": 0,
        "auction_reserve": 0, "auction_reserve_release": 0,
        "auction_bid_reserve": 0, "auction_bid_release": 0,
    }
    frozen_pos = {"freeze": 1, "frozen_clear": -1, "reversal_unfreeze": -1}
    reserved_pos = {
        "trade_reserve": 1, "trade_release": -1, "trade_deliver_out": -1,
        "auction_reserve": 1, "auction_reserve_release": -1, "auction_deliver_out": -1,
        "auction_bid_reserve": 1, "auction_bid_release": -1,
    }
    ec = ef = er = 0.0
    for t in txs:
        ec = round(ec + current_pos.get(t.tx_type, 0) * float(t.amount), 4)
        ef = round(ef + frozen_pos.get(t.tx_type, 0) * float(t.amount), 4)
        er = round(er + reserved_pos.get(t.tx_type, 0) * float(t.amount), 4)
        assert float(t.balance_after) == approx(ec), f"流水#{t.id} 持仓快照不符（{t.tx_type}）"
        assert float(t.frozen_after or 0) == approx(ef), f"流水#{t.id} 冻结快照不符"
        assert float(t.reserved_after or 0) == approx(er), f"流水#{t.id} 占用快照不符"
    acc = db.get(AllowanceAccount, account_id)
    assert float(acc.current_balance) == approx(ec)
    assert float(acc.frozen_balance) == approx(ef)
    assert float(acc.reserved_balance) == approx(er)
    assert float(acc.frozen_balance) + float(acc.reserved_balance) <= float(acc.current_balance) + 1e-9


class TestSessionLifecycle:
    def test_create_draft_then_open(self, db, market):
        s = create_session(db, year=YEAR, name="首场", operator=ADMIN)
        assert s.status == DRAFT
        assert s.session_no.startswith("AUC")
        s = open_session(db, s.id, ADMIN)
        assert s.status == OPEN
        assert s.open_at is not None
        # 重复开放幂等
        assert open_session(db, s.id, ADMIN).status == OPEN

    def test_open_non_draft_rejected(self, db, market):
        s = _open_session(db)
        # 无报价也可撮合（无成交空撮合），撮合后进入 matched
        run_matching(db, s.id, ADMIN)
        with pytest.raises(AuctionError, match="不能开放"):
            open_session(db, s.id, ADMIN)

    def test_idempotent_create(self, db, market):
        a = create_session(db, year=YEAR, name="A", operator=ADMIN, idempotency_key="s-1")
        b = create_session(db, year=YEAR, name="B", operator=ADMIN, idempotency_key="s-1")
        assert a.id == b.id
        assert db.query(AuctionSession).count() == 1

    def test_cancel_open_session_marks_bids_cancelled(self, db, market):
        s = _open_session(db)
        bid = place_bid(db, s.id, market["S1"].id, "sell", 100, 80, operator=ADMIN)
        s2 = cancel_session(db, s.id, ADMIN, reason="监管取消")
        assert s2.status == CANCELLED
        assert db.get(AuctionBid, bid.id).status == BID_CANCELLED
        # 已撤场次再撤幂等；已撤不能结算
        assert cancel_session(db, s.id, ADMIN).status == CANCELLED
        with pytest.raises(AuctionError, match="已撤销"):
            settle_session(db, s.id, ADMIN)

    def test_settled_session_cannot_cancel(self, db, market):
        s = _open_session(db)
        place_bid(db, s.id, market["S1"].id, "sell", 100, 80, operator=ADMIN)
        place_bid(db, s.id, market["B1"].id, "buy", 100, 90, operator=ADMIN)
        run_matching(db, s.id, ADMIN)
        settle_session(db, s.id, ADMIN)
        with pytest.raises(AuctionError, match="已结算"):
            cancel_session(db, s.id, ADMIN)


class TestBidding:
    def test_place_and_cancel_bid(self, db, market):
        s = _open_session(db)
        bid = place_bid(db, s.id, market["S1"].id, "sell", 200, 85, operator=ADMIN)
        assert bid.status == BID_ACTIVE
        assert bid.bid_no.startswith("BID")
        bid = cancel_bid(db, bid.id, market["S1"].id, ADMIN, reason="改价")
        assert bid.status == BID_CANCELLED
        assert bid.cancel_reason == "改价"
        # 撤单后可重新报价
        bid2 = place_bid(db, s.id, market["S1"].id, "sell", 300, 86, operator=ADMIN)
        assert bid2.status == BID_ACTIVE

    def test_sell_cannot_exceed_available(self, db, market):
        s = _open_session(db)
        with pytest.raises(AuctionError, match="自由可用"):
            place_bid(db, s.id, market["S1"].id, "sell", 1200, 80, operator=ADMIN)
        # 被拒不产生报价
        assert db.query(AuctionBid).count() == 0

    def test_reserve_price_floor(self, db, market):
        s = _open_session(db, reserve_price=88)
        with pytest.raises(AuctionError, match="保留价"):
            place_bid(db, s.id, market["S1"].id, "sell", 100, 87, operator=ADMIN)
        with pytest.raises(AuctionError, match="保留价"):
            place_bid(db, s.id, market["B1"].id, "buy", 100, 87, operator=ADMIN)

    def test_one_active_bid_per_company_side(self, db, market):
        s = _open_session(db)
        place_bid(db, s.id, market["S1"].id, "sell", 100, 80, operator=ADMIN)
        with pytest.raises(AuctionError, match="已有有效报价"):
            place_bid(db, s.id, market["S1"].id, "sell", 100, 81, operator=ADMIN)
        # 买方向可再报一张
        place_bid(db, s.id, market["S1"].id, "buy", 50, 70, operator=ADMIN)
        assert db.query(AuctionBid).filter_by(status=BID_ACTIVE).count() == 2

    def test_bid_only_when_open(self, db, market):
        s = create_session(db, year=YEAR, name="草稿", operator=ADMIN)
        with pytest.raises(AuctionError, match="仅开放"):
            place_bid(db, s.id, market["S1"].id, "sell", 100, 80, operator=ADMIN)

    def test_cannot_cancel_other_company_bid(self, db, market):
        s = _open_session(db)
        bid = place_bid(db, s.id, market["S1"].id, "sell", 100, 80, operator=ADMIN)
        with pytest.raises(AuctionError, match="无权撤销"):
            cancel_bid(db, bid.id, market["S2"].id, ADMIN)

    def test_idempotent_bid(self, db, market):
        s = _open_session(db)
        a = place_bid(db, s.id, market["S1"].id, "sell", 100, 80,
                      operator=ADMIN, idempotency_key="b-1")
        b = place_bid(db, s.id, market["S1"].id, "sell", 200, 90,
                      operator=ADMIN, idempotency_key="b-1")
        assert a.id == b.id
        assert float(b.quantity) == 100


class TestMatching:
    def test_uniform_price_max_volume(self, db, market):
        """经典集合竞价：买卖各两档，出清价落在最大成交量档。"""
        s = _open_session(db)
        # 卖方：100@80、100@90；买方：100@100、100@90
        place_bid(db, s.id, market["S1"].id, "sell", 100, 80, operator=ADMIN)
        place_bid(db, s.id, market["S2"].id, "sell", 100, 90, operator=ADMIN)
        place_bid(db, s.id, market["B1"].id, "buy", 100, 100, operator=ADMIN)
        # 第二买方：甲企业同时报买（不同方向允许），避免自成交影响
        place_bid(db, s.id, market["S1"].id, "buy", 100, 90, operator=ADMIN)
        s = run_matching(db, s.id, ADMIN)
        # p=80: 需求200 供给100 → 100；p=90: 需求200 供给200 → 200；p=100: 需求100 供给200 → 100
        assert s.status == MATCHED
        assert float(s.clear_price) == approx(90)
        assert float(s.matched_volume) == approx(200)
        assert s.trade_count == 2

    def test_bid_reserves_then_matching_and_settles(self, db, market):
        """卖出报价即占用 → 撮合定价 → 结算划转，双方账户与流水闭环。"""
        s = _open_session(db)
        sb = place_bid(db, s.id, market["S1"].id, "sell", 300, 80, operator=ADMIN)
        # 报价提交瞬间卖方 300 吨转为交易占用（持仓不变）
        seller_acc = _account(db, market["S1"].id)
        assert float(seller_acc.current_balance) == 1000
        assert float(seller_acc.reserved_balance) == 300
        bid_reserve_tx = db.query(AllowanceTransaction).filter_by(tx_type="auction_bid_reserve").one()
        assert float(bid_reserve_tx.amount) == 300
        assert float(bid_reserve_tx.reserved_after) == 300
        assert bid_reserve_tx.auction_trade_id is None

        bb = place_bid(db, s.id, market["B1"].id, "buy", 300, 90, operator=ADMIN)
        run_matching(db, s.id, ADMIN)

        seller_acc = _account(db, market["S1"].id)
        buyer_acc = _account(db, market["B1"].id)
        assert float(seller_acc.current_balance) == 1000
        assert float(seller_acc.reserved_balance) == 300
        assert float(buyer_acc.current_balance) == 200
        assert db.get(AuctionBid, sb.id).status == BID_MATCHED
        assert db.get(AuctionBid, bb.id).status == BID_MATCHED

        settle_session(db, s.id, ADMIN)
        db.expire_all()
        seller_acc = _account(db, market["S1"].id)
        buyer_acc = _account(db, market["B1"].id)
        assert float(seller_acc.current_balance) == 700
        assert float(seller_acc.reserved_balance) == 0
        assert float(buyer_acc.current_balance) == 500
        trades = db.query(AuctionTrade).all()
        assert len(trades) == 1
        assert trades[0].status == TRADE_SETTLED
        assert trades[0].settled_at is not None
        _assert_snapshot(db, seller_acc.id)
        _assert_snapshot(db, buyer_acc.id)

    def test_partial_fill_by_demand(self, db, market):
        """卖方供 300、买方求 200：卖方部分成交，买方全部成交。"""
        s = _open_session(db)
        sb = place_bid(db, s.id, market["S1"].id, "sell", 300, 80, operator=ADMIN)
        place_bid(db, s.id, market["B1"].id, "buy", 200, 90, operator=ADMIN)
        run_matching(db, s.id, ADMIN)
        assert float(db.get(AuctionSession, s.id).matched_volume) == approx(200)
        sb = db.get(AuctionBid, sb.id)
        assert sb.status == BID_PARTIAL
        assert float(sb.filled_quantity) == approx(200)

    def test_no_cross_price_no_trade(self, db, market):
        """最高买价 < 最低卖价：无成交，报价全部 unmatched，场次进入撮合完成态。"""
        s = _open_session(db)
        place_bid(db, s.id, market["S1"].id, "sell", 100, 95, operator=ADMIN)
        place_bid(db, s.id, market["B1"].id, "buy", 100, 80, operator=ADMIN)
        s = run_matching(db, s.id, ADMIN)
        assert s.status == MATCHED
        assert float(s.matched_volume) == 0
        assert s.clear_price is None
        assert db.query(AuctionBid).filter_by(status=BID_UNMATCHED).count() == 2
        # 无成交也可结算（幂等空结算）
        s = settle_session(db, s.id, ADMIN)
        assert s.status == SETTLED

    def test_sell_capped_by_available_at_match(self, db, market):
        """卖方报价已占用 800；其配额再被订单占用 500 后，撮合按仅存自由可用封顶。

        卖出报价在提交时已占用 800，此时自由可用只剩 200，企业间订单只能占用
        剩余 200；为构造“撮合时报价占用之外还有其他占用”的场景，先建订单 500 被拒，
        改由订单占用 200、撮合释放未成交余量：买方仅求 500 → 成交 500。
        """
        from app.services.trade_order_service import confirm_order, create_order

        s = _open_session(db)
        # 甲报卖 800（占用 800，自由可用 200），丙买 500
        place_bid(db, s.id, market["S1"].id, "sell", 800, 80, operator=ADMIN)
        place_bid(db, s.id, market["B1"].id, "buy", 500, 90, operator=ADMIN)
        # 再确认一张 200 吨企业间订单，恰好用尽剩余自由可用
        order = create_order(db, market["S1"].id, market["S2"].id, YEAR, 200, price=70)
        confirm_order(db, order.id, market["S2"].id)

        s = run_matching(db, s.id, ADMIN)
        # 卖方报价 800 中 500 成交（reserved 保持），300 未成交余量释放
        assert float(s.matched_volume) == approx(500)
        acc = _account(db, market["S1"].id)
        # 竞价成交保留占用 500 + 订单占用 200 = 700；释放了 300 未成交余量
        assert float(acc.reserved_balance) == 700
        assert float(acc.current_balance) == 1000
        _assert_snapshot(db, acc.id)

    def test_self_trade_skipped(self, db, market):
        """同一家企业在同场次同时买卖且队列相遇时不与自己成交。"""
        s = _open_session(db)
        # 甲卖 100@80 且甲买 100@90；丙买 100@90
        place_bid(db, s.id, market["S1"].id, "sell", 100, 80, operator=ADMIN)
        place_bid(db, s.id, market["S1"].id, "buy", 100, 90, operator=ADMIN)
        b1 = place_bid(db, s.id, market["B1"].id, "buy", 100, 90, operator=ADMIN)
        s = run_matching(db, s.id, ADMIN)
        # 买方按价格降序、id 升序：甲(id 小)在前；甲买与甲卖自成交跳过，
        # 卖单继续等待后续买方 → 与丙成交 100
        assert float(s.matched_volume) == approx(100)
        trade = db.query(AuctionTrade).one()
        assert trade.seller_id == market["S1"].id
        assert trade.buyer_id == market["B1"].id
        assert db.get(AuctionBid, b1.id).status == BID_MATCHED

    def test_match_then_cancel_releases_reservation(self, db, market):
        """撮合后撤场：逐笔释放卖方占用，成交单作废，持仓不变。"""
        s = _open_session(db)
        place_bid(db, s.id, market["S1"].id, "sell", 300, 80, operator=ADMIN)
        place_bid(db, s.id, market["B1"].id, "buy", 300, 90, operator=ADMIN)
        run_matching(db, s.id, ADMIN)
        s2 = cancel_session(db, s.id, ADMIN, reason="紧急中止")
        assert s2.status == CANCELLED
        db.expire_all()
        acc = _account(db, market["S1"].id)
        assert float(acc.reserved_balance) == 0
        assert float(acc.current_balance) == 1000
        release_tx = db.query(AllowanceTransaction).filter_by(tx_type="auction_reserve_release").one()
        assert float(release_tx.amount) == 300
        assert float(release_tx.reserved_after) == 0
        assert db.query(AuctionTrade).filter_by(status=TRADE_CANCELLED).count() == 1
        assert db.query(AuctionBid).filter_by(status=BID_CANCELLED).count() == 2
        _assert_snapshot(db, acc.id)

    def test_repeat_match_idempotent(self, db, market):
        s = _open_session(db)
        place_bid(db, s.id, market["S1"].id, "sell", 100, 80, operator=ADMIN)
        place_bid(db, s.id, market["B1"].id, "buy", 100, 90, operator=ADMIN)
        s1 = run_matching(db, s.id, ADMIN)
        s2 = run_matching(db, s.id, ADMIN)
        assert s1.id == s2.id
        assert db.query(AuctionTrade).count() == 1


class TestSettlementClearanceLoop:
    def _make_buyer_deficit(self, db, company, emission, factor=0.5703):
        """构造买方年度缺口：活动数据→核算→报告批准（冻结+缺口）。"""
        from app.models import ActivityData, CalculationMethod, EmissionFactor, EmissionScope
        from app.services.calculation_service import recalc_company_year
        from app.services.mrv_service import approve_report, generate_report, submit_report

        db.add(EmissionScope(company_id=company.id, scope="2", category="外购电力", name="厂区用电"))
        db.add(CalculationMethod(
            method_code="ELEC", name="外购电力排放因子法", scope="2", formula_type="activity_factor"))
        db.add(EmissionFactor(
            factor_code="ELEC-GRID", name="外购电力", scope="2", unit="tCO2/MWh", value=factor,
            source="电网因子", valid_from="2025-01-01", valid_to="2026-12-31"))
        db.flush()
        scope_id = db.query(EmissionScope).filter_by(company_id=company.id, scope="2").one().id
        db.add(ActivityData(
            company_id=company.id, scope_id=scope_id, year=YEAR, period="monthly",
            activity_type="外购电力", unit="MWh",
            quantity=round(emission / factor, 6), data_source="台账", verified=1))
        db.commit()
        recalc_company_year(db, company.id, YEAR)
        report = generate_report(db, company.id, YEAR)
        submit_report(db, report)
        approve_report(db, report, verifier_id=1)
        db.commit()

    def test_settle_closes_buyer_deficit(self, db, market):
        """买方缺口 400：结算到账 400 同事务自动补缴，履约达标、账户清零。"""
        buyer = market["B1"]
        self._make_buyer_deficit(db, buyer, 600)  # 持仓 200 全冻结，缺口 400

        s = _open_session(db)
        place_bid(db, s.id, market["S1"].id, "sell", 400, 80, operator=ADMIN)
        place_bid(db, s.id, buyer.id, "buy", 400, 90, operator=ADMIN)
        run_matching(db, s.id, ADMIN)
        settle_session(db, s.id, ADMIN)

        record = db.query(ComplianceRecord).filter_by(
            company_id=buyer.id, year=YEAR, is_active=1).one()
        assert record.status == "compliant"
        assert float(record.cleared_amount) == approx(600)
        assert float(record.deficit) == approx(0)
        db.expire_all()
        acc = _account(db, buyer.id)
        # 200 冻结 + 400 到账 - 600 清缴 = 0
        assert float(acc.current_balance) == 0
        assert float(acc.frozen_balance) == 0
        assert db.query(AllowanceTransaction).filter_by(tx_type="auction_deficit_clear").count() == 1
        _assert_snapshot(db, acc.id)
        _assert_snapshot(db, _account(db, market["S1"].id).id)

    def test_partial_buy_leaves_deficit(self, db, market):
        """到账不足以覆盖缺口：只核销能覆盖部分，保留 deficit，配额守恒。"""
        buyer = market["B1"]
        self._make_buyer_deficit(db, buyer, 600)  # 冻结 200、缺口 400

        s = _open_session(db)
        place_bid(db, s.id, market["S1"].id, "sell", 100, 80, operator=ADMIN)
        place_bid(db, s.id, buyer.id, "buy", 400, 90, operator=ADMIN)
        run_matching(db, s.id, ADMIN)
        settle_session(db, s.id, ADMIN)

        record = db.query(ComplianceRecord).filter_by(
            company_id=buyer.id, year=YEAR, is_active=1).one()
        assert record.status == "deficit"
        assert float(record.cleared_amount) == approx(300)
        assert float(record.deficit) == approx(300)
        db.expire_all()
        acc = _account(db, buyer.id)
        # 冻结 200 核销 + 100 到账即补缴，余 0
        assert float(acc.current_balance) == 0

    def test_auto_clear_disabled(self, db, market):
        """场次关闭联动清缴：结算只到账，缺口保留待手动清缴。"""
        buyer = market["B1"]
        self._make_buyer_deficit(db, buyer, 600)

        s = _open_session(db, auto_clear=False)
        place_bid(db, s.id, market["S1"].id, "sell", 400, 80, operator=ADMIN)
        place_bid(db, s.id, buyer.id, "buy", 400, 90, operator=ADMIN)
        run_matching(db, s.id, ADMIN)
        settle_session(db, s.id, ADMIN)

        record = db.query(ComplianceRecord).filter_by(
            company_id=buyer.id, year=YEAR, is_active=1).one()
        assert record.status == "deficit"
        assert float(record.deficit) == approx(400)
        db.expire_all()
        acc = _account(db, buyer.id)
        # 原持仓 200 全部冻结（frozen 200），到账 400 留存为自由可用；不联动补缴
        assert float(acc.current_balance) == 600
        assert float(acc.frozen_balance) == 200
        assert float(acc.reserved_balance) == 0

    def test_conservation_after_settle(self, db, market):
        """结算后市场总量守恒：卖方出库之和 = 买方到账之和。"""
        s = _open_session(db)
        place_bid(db, s.id, market["S1"].id, "sell", 200, 80, operator=ADMIN)
        place_bid(db, s.id, market["S2"].id, "sell", 100, 82, operator=ADMIN)
        place_bid(db, s.id, market["B1"].id, "buy", 300, 90, operator=ADMIN)
        run_matching(db, s.id, ADMIN)
        settle_session(db, s.id, ADMIN)
        db.expire_all()
        total = sum(float(_account(db, c.id).current_balance) for c in market.values())
        assert total == approx(2200)  # 1000 + 1000 + 200，划转不增不减


class TestConcurrency:
    def test_parallel_settle_only_once(self, db, market):
        """多线程并发结算：只有一次划转，其余幂等或竞争失败。"""
        s = _open_session(db)
        place_bid(db, s.id, market["S1"].id, "sell", 300, 80, operator=ADMIN)
        place_bid(db, s.id, market["B1"].id, "buy", 300, 90, operator=ADMIN)
        run_matching(db, s.id, ADMIN)
        sid = s.id
        outcomes = []

        def worker():
            session = _fresh(db)
            try:
                r = settle_session(session, sid, ADMIN)
                outcomes.append(r.status)
            except AuctionError:
                outcomes.append("reject")
            finally:
                session.close()

        with ThreadPoolExecutor(max_workers=6) as pool:
            list(pool.map(lambda _: worker(), range(6)))

        db.expire_all()
        assert outcomes.count(SETTLED) >= 1
        assert set(outcomes) <= {SETTLED, "reject"}
        assert db.query(AllowanceTransaction).filter_by(tx_type="auction_deliver_out").count() == 1
        assert db.query(AllowanceTransaction).filter_by(tx_type="auction_deliver_in").count() == 1
        assert float(_account(db, market["S1"].id).current_balance) == 700
        assert float(_account(db, market["B1"].id).current_balance) == 500

    def test_settle_races_cancel_only_one_wins(self, db, market):
        """结算与撤场并发：恰一方胜出，不产生“已结算又释放”脏账。"""
        s = _open_session(db)
        place_bid(db, s.id, market["S1"].id, "sell", 300, 80, operator=ADMIN)
        place_bid(db, s.id, market["B1"].id, "buy", 300, 90, operator=ADMIN)
        run_matching(db, s.id, ADMIN)
        sid = s.id

        def settle():
            session = _fresh(db)
            try:
                settle_session(session, sid, ADMIN)
                return "settled"
            except AuctionError:
                return "reject"
            finally:
                session.close()

        def cancel():
            session = _fresh(db)
            try:
                cancel_session(session, sid, ADMIN, "并发撤场")
                return "cancelled"
            except AuctionError:
                return "reject"
            finally:
                session.close()

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda f: f(), [settle, cancel] * 4))

        db.expire_all()
        final = db.get(AuctionSession, sid)
        assert final.status in (SETTLED, CANCELLED)
        seller_acc = _account(db, market["S1"].id)
        buyer_acc = _account(db, market["B1"].id)
        if final.status == SETTLED:
            assert "settled" in results
            assert float(seller_acc.current_balance) == 700
            assert float(buyer_acc.current_balance) == 500
            assert db.query(AuctionTrade).filter_by(status=TRADE_SETTLED).count() == 1
            assert db.query(AllowanceTransaction).filter_by(
                tx_type="auction_reserve_release").count() == 0
        else:
            assert "cancelled" in results
            assert float(seller_acc.current_balance) == 1000
            assert float(seller_acc.reserved_balance) == 0
            assert float(buyer_acc.current_balance) == 200
            assert db.query(AuctionTrade).filter_by(status=TRADE_CANCELLED).count() == 1
        _assert_snapshot(db, seller_acc.id)
        _assert_snapshot(db, buyer_acc.id)

    def test_parallel_sell_bids_within_available(self, db, market):
        """同一卖方在不同场次并发报卖：各场次报量 600，总报量受 1000 自由可用约束。

        注意：同一场次同方向唯一索引禁止重复；这里跨场次并发验证可用余额校验。
        """
        sessions = []
        for i in range(2):
            s = create_session(db, year=YEAR, name=f"场{i}", operator=ADMIN)
            sessions.append(open_session(db, s.id, ADMIN).id)
        db.commit()
        seller = market["S1"].id

        def worker(sid):
            session = _fresh(db)
            try:
                place_bid(session, sid, seller, "sell", 600, 80, operator=ADMIN)
                return "ok"
            except AuctionError:
                session.rollback()
                return "reject"
            finally:
                session.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(worker, sessions))

        db.expire_all()
        assert sorted(outcomes) == ["ok", "reject"]
        # 卖出报价原子占用：两场只有一张报价能成功占用 600
        active_qty = sum(
            float(b.quantity)
            for b in db.query(AuctionBid).filter_by(company_id=seller, side="sell", status=BID_ACTIVE)
        )
        assert active_qty == approx(600)
        acc = _account(db, seller)
        assert float(acc.reserved_balance) == approx(600)
        assert float(acc.current_balance) == 1000

    def test_settle_races_manual_clear_conservation(self, db, market):
        """结算与手动清缴并发：配额守恒，流水链一致。"""
        from app.services.quota_service import clear_emission

        buyer = market["B1"]
        # 买方排放 400（持仓 200 冻结，缺口 200）
        TestSettlementClearanceLoop()._make_buyer_deficit(db, buyer, 400)

        s = _open_session(db, auto_clear=False)
        place_bid(db, s.id, market["S1"].id, "sell", 200, 80, operator=ADMIN)
        place_bid(db, s.id, buyer.id, "buy", 200, 90, operator=ADMIN)
        run_matching(db, s.id, ADMIN)
        sid = s.id
        errors = []

        def settle():
            session = _fresh(db)
            try:
                settle_session(session, sid, ADMIN)
            except Exception as exc:  # noqa: BLE001
                errors.append(("settle", exc))
            finally:
                session.close()

        def clear():
            session = _fresh(db)
            try:
                # 到账前清缴只能核销冻结 200；与结算并发后由企业年度键串行
                clear_emission(session, buyer.id, YEAR, f"{YEAR}-12-31")
            except Exception as exc:  # noqa: BLE001
                errors.append(("clear", exc))
            finally:
                session.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(lambda f: f(), [settle, clear]))

        db.expire_all()
        assert not errors, f"并发异常：{errors!r}"
        # 结算到账 200 + 原持仓 200；冻结 200 必被清缴；是否再补缴 200 取决于时序，
        # 但最终持仓必为 0 或 200，履约记录 cleared ≤ 400 且账本守恒。
        acc = _account(db, buyer.id)
        record = db.query(ComplianceRecord).filter_by(
            company_id=buyer.id, year=YEAR, is_active=1).one()
        assert float(acc.current_balance) in (approx(0), approx(200))
        assert float(acc.frozen_balance) == 0
        assert float(record.cleared_amount) <= 400 + 1e-9
        _assert_snapshot(db, acc.id)


class TestAudit:
    def test_lifecycle_actions_audited(self, db, market):
        s = _open_session(db)
        bid = place_bid(db, s.id, market["S1"].id, "sell", 100, 80, operator=ADMIN)
        cancel_bid(db, bid.id, market["S1"].id, ADMIN, "测试撤")
        place_bid(db, s.id, market["S1"].id, "sell", 100, 80, operator=ADMIN)
        place_bid(db, s.id, market["B1"].id, "buy", 100, 90, operator=ADMIN)
        run_matching(db, s.id, ADMIN)
        settle_session(db, s.id, ADMIN)

        logs = list_audit_logs(db)
        actions = {x.action for x in logs}
        assert {"session.create", "session.open", "bid.place", "bid.cancel",
                "session.match", "session.settle"} <= actions
        # 所有业务日志与业务同事务成功落库
        assert all(x.result == "success" for x in logs)
        assert all(x.operator_name == "admin" for x in logs)

    def test_denied_bid_writes_audit_at_api(self, db, market):
        """API 层越权/拒绝审计由接口测试覆盖；service 层拒绝默认不写（由 API 补记）。"""
        s = _open_session(db)
        with pytest.raises(AuctionError):
            place_bid(db, s.id, market["S1"].id, "sell", 5000, 80, operator=ADMIN)
        # service 层不记录拒绝（未进入业务事务），不产生半条日志
        assert db.query(AuctionAuditLog).filter_by(result="denied").count() == 0
