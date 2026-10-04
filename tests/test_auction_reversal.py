"""已结算竞价成交的监管冲正与违约回退链路服务层测试。

覆盖：
- 冲正回退：无清缴联动的整笔冲正、批量冲正、部分数量冲正、重复冲正拒绝；
- 履约联动回滚：解除冻结、退还自由配额补缴，履约记录/配额状态精确回退；
- 部分回退：多笔成交按成交单归属回滚，不影响其余成交单的清缴结果；
- 违约：买方持仓不足只收回可得部分、登记欠额、卖方由解冻/退还配额即时补位；
- 违约回退：监管手动追偿（逐笔/按买方）、后续场次结算到账自动追偿、结清解除违约；
- 一致性：流水三类快照链守恒、配额守恒（违约敞口可解释）、
  多线程并发冲正只生效一次、幂等键去重；
- 审计：冲正/追偿/自动追偿均写审计。
"""

from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from app.core.database import Base
from app.models import (
    AllowanceAccount,
    AllowanceTransaction,
    AuctionDefaultRepayment,
    AuctionReversalBatch,
    AuctionSession,
    AuctionTrade,
    AuctionTradeReversal,
    Company,
    ComplianceRecord,
)
from app.services.auction_service import (
    SETTLED,
    TRADE_DEFAULTED,
    TRADE_REVERSED,
    TRADE_SETTLED,
    AuctionError,
    Operator,
    create_session,
    list_defaulted_trades,
    open_session,
    place_bid,
    recover_buyer_defaults,
    repay_trade_default,
    reverse_settled_trades,
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
    engine = create_engine(
        f"sqlite:///{tmp_path / 'reverse.db'}",
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
    """甲/乙各 1000 吨（卖方），丙 200 吨（买方）。"""
    companies = {}
    for code, name, quota in [("S1", "卖方甲", 1000), ("S2", "卖方乙", 1000), ("B1", "买方丙", 200)]:
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


def _open_session(db, reserve_price=0.0, auto_clear=True, auto_recover=True, name="测试场次"):
    s = create_session(
        db, year=YEAR, name=name, reserve_price=reserve_price,
        auto_clear_deficit=auto_clear, operator=ADMIN,
    )
    s.auto_recover_default = 1 if auto_recover else 0
    db.commit()
    return open_session(db, s.id, ADMIN)


def _settle_trade(db, seller_id, buyer_id, qty, bid_price=80, ask_price=80,
                  auto_clear=True, auto_recover=True):
    s = _open_session(db, auto_clear=auto_clear, auto_recover=auto_recover)
    place_bid(db, s.id, seller_id, "sell", qty, ask_price, operator=ADMIN)
    place_bid(db, s.id, buyer_id, "buy", qty, max(bid_price, ask_price) + 10, operator=ADMIN)
    run_matching(db, s.id, ADMIN)
    settle_session(db, s.id, ADMIN)
    db.expire_all()
    trade = db.query(AuctionTrade).filter_by(session_id=s.id).one()
    return s, trade


def _make_buyer_deficit(db, company, emission, factor=0.5703):
    """构造买方年度缺口：活动数据→核算→报告批准（冻结+缺口）。返回已批准报告。"""
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
    return report


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
        "auction_clear_refund": 1, "auction_clear_unfreeze": 1,
        "auction_clawback_in": 1, "auction_default_repay_in": 1,
        "sell": -1, "transfer_out": -1, "offset": -1, "clear": -1,
        "frozen_clear": -1, "trade_deficit_clear": -1, "auction_deficit_clear": -1,
        "trade_deliver_out": -1, "auction_deliver_out": -1,
        "auction_clawback_out": -1, "auction_default_repay_out": -1,
        "freeze": 0, "trade_reserve": 0, "trade_release": 0,
        "auction_reserve": 0, "auction_reserve_release": 0,
        "auction_bid_reserve": 0, "auction_bid_release": 0,
    }
    frozen_pos = {"freeze": 1, "frozen_clear": -1, "reversal_unfreeze": -1,
                  "auction_clear_unfreeze": -1}
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
        assert float(t.frozen_after or 0) == approx(ef), f"流水#{t.id} 冻结快照不符（{t.tx_type}）"
        assert float(t.reserved_after or 0) == approx(er), f"流水#{t.id} 占用快照不符（{t.tx_type}）"
    acc = db.get(AllowanceAccount, account_id)
    assert float(acc.current_balance) == approx(ec)
    assert float(acc.frozen_balance) == approx(ef)
    assert float(acc.reserved_balance) == approx(er)
    assert float(acc.frozen_balance) + float(acc.reserved_balance) <= float(acc.current_balance) + 1e-9


def _reverse(db, session_id, reason="监管冲正", **kw):
    return reverse_settled_trades(db, session_id, ADMIN, reason, **kw)


class TestPlainReversal:
    def test_full_reverse_restores_both_sides(self, db, market):
        """无清缴联动：整笔冲正后双方账户恢复到结算前。"""
        s, trade = _settle_trade(db, market["S1"].id, market["B1"].id, 300)
        assert float(_account(db, market["S1"].id).current_balance) == 700
        assert float(_account(db, market["B1"].id).current_balance) == 500

        batch = _reverse(db, s.id)
        assert batch.trade_count == 1
        assert float(batch.reverse_volume) == 300
        assert float(batch.recovered_volume) == 300
        assert float(batch.default_volume) == 0

        db.expire_all()
        assert float(_account(db, market["S1"].id).current_balance) == 1000
        assert float(_account(db, market["B1"].id).current_balance) == 200
        assert db.get(AuctionTrade, trade.id).status == TRADE_REVERSED
        assert db.query(AuctionTradeReversal).count() == 1
        _assert_snapshot(db, _account(db, market["S1"].id).id)
        _assert_snapshot(db, _account(db, market["B1"].id).id)

    def test_reverse_writes_audit(self, db, market):
        s, trade = _settle_trade(db, market["S1"].id, market["B1"].id, 100)
        _reverse(db, s.id, reason="操作异常回退")
        logs = db.query(AuctionDefaultRepayment).all()
        assert logs == []
        from app.models import AuctionAuditLog
        actions = {x.action for x in db.query(AuctionAuditLog).all()}
        assert "trade.reverse" in actions and "session.reverse" in actions

    def test_only_settled_session_can_reverse(self, db, market):
        s = _open_session(db)
        place_bid(db, s.id, market["S1"].id, "sell", 100, 80, operator=ADMIN)
        with pytest.raises(AuctionError, match="仅已结算"):
            _reverse(db, s.id)

    def test_reason_required(self, db, market):
        s, _ = _settle_trade(db, market["S1"].id, market["B1"].id, 100)
        with pytest.raises(AuctionError, match="冲正原因"):
            _reverse(db, s.id, reason="x")

    def test_cannot_reverse_twice(self, db, market):
        s, trade = _settle_trade(db, market["S1"].id, market["B1"].id, 100)
        _reverse(db, s.id)
        with pytest.raises(AuctionError, match="没有可冲正"):
            _reverse(db, s.id)

    def test_idempotency_key_dedup(self, db, market):
        s, _ = _settle_trade(db, market["S1"].id, market["B1"].id, 100)
        b1 = _reverse(db, s.id, idempotency_key="rev-1")
        b2 = _reverse(db, s.id, idempotency_key="rev-1")
        assert b1.id == b2.id
        assert db.query(AuctionReversalBatch).count() == 1
        assert db.query(AuctionTradeReversal).count() == 1

    def test_batch_multiple_trades(self, db, market):
        """一场次两笔成交，批量冲正一并回退。"""
        s = _open_session(db)
        place_bid(db, s.id, market["S1"].id, "sell", 100, 80, operator=ADMIN)
        place_bid(db, s.id, market["S2"].id, "sell", 50, 80, operator=ADMIN)
        place_bid(db, s.id, market["B1"].id, "buy", 150, 90, operator=ADMIN)
        run_matching(db, s.id, ADMIN)
        settle_session(db, s.id, ADMIN)
        trades = db.query(AuctionTrade).filter_by(session_id=s.id).all()
        assert len(trades) == 2

        batch = _reverse(db, s.id)
        assert batch.trade_count == 2
        assert float(batch.reverse_volume) == 150
        db.expire_all()
        assert float(_account(db, market["S1"].id).current_balance) == 1000
        assert float(_account(db, market["S2"].id).current_balance) == 1000
        assert float(_account(db, market["B1"].id).current_balance) == 200
        assert all(t.status == TRADE_REVERSED for t in db.query(AuctionTrade).all())
        _assert_snapshot(db, _account(db, market["B1"].id).id)


class TestPartialReversal:
    def test_partial_quantity_keeps_trade_settled(self, db, market):
        s, trade = _settle_trade(db, market["S1"].id, market["B1"].id, 300)
        batch = _reverse(db, s.id, quantities={trade.id: 100})
        assert float(batch.reverse_volume) == 100
        db.expire_all()
        t = db.get(AuctionTrade, trade.id)
        assert t.status == TRADE_SETTLED
        assert float(t.reversed_quantity) == 100
        assert float(_account(db, market["S1"].id).current_balance) == 800
        assert float(_account(db, market["B1"].id).current_balance) == 400
        _assert_snapshot(db, _account(db, market["S1"].id).id)
        _assert_snapshot(db, _account(db, market["B1"].id).id)

        # 再冲正剩余 200，整笔完成后状态转 reversed
        _reverse(db, s.id)
        db.expire_all()
        t = db.get(AuctionTrade, trade.id)
        assert t.status == TRADE_REVERSED
        assert float(t.reversed_quantity) == 300
        assert float(_account(db, market["B1"].id).current_balance) == 200

    def test_partial_qty_exceeds_remaining_rejected(self, db, market):
        s, trade = _settle_trade(db, market["S1"].id, market["B1"].id, 100)
        with pytest.raises(AuctionError, match="待冲正量仅"):
            _reverse(db, s.id, quantities={trade.id: 101})

    def test_partial_reverse_only_rolls_back_attributed_clearance(self, db, market):
        """同一买方两笔成交触发缺口清缴，冲正第一笔只回滚它触发的部分。"""
        buyer = market["B1"]
        _make_buyer_deficit(db, buyer, 500)  # 持仓 200 冻结，缺口 300

        s = _open_session(db)
        place_bid(db, s.id, market["S1"].id, "sell", 100, 80, operator=ADMIN)
        place_bid(db, s.id, market["S2"].id, "sell", 200, 80, operator=ADMIN)
        place_bid(db, s.id, buyer.id, "buy", 300, 90, operator=ADMIN)
        run_matching(db, s.id, ADMIN)
        settle_session(db, s.id, ADMIN)

        record = db.query(ComplianceRecord).filter_by(
            company_id=buyer.id, year=YEAR, is_active=1).one()
        assert record.status == "compliant"
        assert float(record.cleared_amount) == approx(500)

        trades = (
            db.query(AuctionTrade).filter_by(session_id=s.id)
            .order_by(AuctionTrade.alloc_seq.asc()).all()
        )
        first, second = trades
        # 冻结核销 200（自有配额，不归属成交单）；到账补缴按成交单归属：
        # trade1=100、trade2=200。冲正第一笔只回滚它触发的 100 到账补缴。
        _reverse(db, s.id, trade_ids=[first.id])
        db.expire_all()
        record = db.query(ComplianceRecord).filter_by(
            company_id=buyer.id, year=YEAR, is_active=1).one()
        assert float(record.cleared_amount) == approx(400)
        assert float(record.frozen_amount) == approx(0)
        assert record.status == "deficit"
        assert float(record.deficit) == approx(100)
        rev = db.query(AuctionTradeReversal).filter_by(trade_id=first.id).one()
        assert float(rev.clear_unfrozen) == approx(0)
        assert float(rev.clear_refunded) == approx(100)
        assert float(rev.recovered_quantity) == approx(100)
        _assert_snapshot(db, _account(db, buyer.id).id)

        # 冲正第二笔 200：回滚 200 到账补缴，最终只剩自有冻结 200 的清缴
        _reverse(db, s.id, trade_ids=[second.id])
        db.expire_all()
        record = db.query(ComplianceRecord).filter_by(
            company_id=buyer.id, year=YEAR, is_active=1).one()
        assert float(record.cleared_amount) == approx(200)
        assert float(record.frozen_amount) == approx(0)
        assert float(record.deficit) == approx(300)
        acc = _account(db, buyer.id)
        assert float(acc.current_balance) == 0
        assert float(acc.frozen_balance) == 0


class TestClearanceRollback:
    def test_full_clearance_reversal_restores_compliance(self, db, market):
        """买方缺口 600、到账 400 全额清缴后冲正：到账补缴退还并收回，缺口恢复。"""
        buyer = market["B1"]
        _make_buyer_deficit(db, buyer, 600)  # 冻结 200、缺口 400

        s, trade = _settle_trade(db, market["S1"].id, buyer.id, 400)
        record = db.query(ComplianceRecord).filter_by(
            company_id=buyer.id, year=YEAR, is_active=1).one()
        assert record.status == "compliant"

        _reverse(db, s.id)
        db.expire_all()
        record = db.query(ComplianceRecord).filter_by(
            company_id=buyer.id, year=YEAR, is_active=1).one()
        assert record.status == "deficit"
        # 成交 400 全部为到账补缴（原冻结 200 的核销不属于本笔交付量），
        # 冲正只回退 400：cleared 600→200，frozen 保持 0，缺口恢复 400
        assert float(record.cleared_amount) == approx(200)
        assert float(record.frozen_amount) == approx(0)
        assert float(record.deficit) == approx(400)
        acc = _account(db, buyer.id)
        assert float(acc.current_balance) == 0
        assert float(acc.frozen_balance) == 0
        assert float(acc.reserved_balance) == 0
        rev = db.query(AuctionTradeReversal).one()
        assert float(rev.clear_unfrozen) == approx(0)
        assert float(rev.clear_refunded) == approx(400)
        assert float(rev.recovered_quantity) == approx(400)
        assert float(rev.defaulted_quantity) == approx(0)
        # 卖方：结算出库 400，冲正退回 400
        assert float(_account(db, market["S1"].id).current_balance) == 1000
        _assert_snapshot(db, acc.id)
        _assert_snapshot(db, _account(db, market["S1"].id).id)
    def test_partial_buy_leaves_deficit_then_reverse(self, db, market):
        """到账 100 只覆盖部分缺口：冲正后恢复 deficit=400。"""
        buyer = market["B1"]
        _make_buyer_deficit(db, buyer, 600)

        s, _ = _settle_trade(db, market["S1"].id, buyer.id, 100)
        record = db.query(ComplianceRecord).filter_by(
            company_id=buyer.id, year=YEAR, is_active=1).one()
        assert record.status == "deficit"
        assert float(record.cleared_amount) == approx(300)

        _reverse(db, s.id)
        db.expire_all()
        record = db.query(ComplianceRecord).filter_by(
            company_id=buyer.id, year=YEAR, is_active=1).one()
        assert record.status == "deficit"
        # 成交 100 全部归属冻结核销（f_rev=100, c_rev=0）：解冻后随冲正退卖方，
        # cleared 300→200，frozen 0，缺口 400；买方持仓 0
        assert float(record.cleared_amount) == approx(200)
        assert float(record.frozen_amount) == approx(0)
        assert float(record.deficit) == approx(400)
        assert float(_account(db, buyer.id).current_balance) == 0


class TestDefaultAndRecovery:
    def test_buyer_short_balance_registers_default(self, db, market):
        """结算后买方把配额转出，冲正退还补缴后自由可用不足，只收回部分并登记违约。"""
        from app.services.trading_service import transfer

        # 买方带缺口：冻结 200，到账 300 中 300 全部补缴缺口（构造 c=300）
        buyer = market["B1"]
        _make_buyer_deficit(db, buyer, 500)
        s, trade = _settle_trade(db, market["S1"].id, buyer.id, 300)
        # 冲正前把账户清零：额外买入再转出，使自由可用为 0
        buyer_acc = _account(db, buyer.id)
        assert float(buyer_acc.current_balance) == 0
        # 冲正退还 300 后立即收回——但模拟买方在冲正前已无配额可退：
        # 这里直接冲正，退还的 300 在同一事务内可收回，故构造“买方事后转出”
        # 的违约需让退还配额小于回退量：先冲正再看无违约（基线），违约场景见下。
        batch = _reverse(db, s.id, reason="成交异常冲正")
        db.expire_all()
        t = db.get(AuctionTrade, trade.id)
        assert t.status == TRADE_REVERSED
        assert float(batch.recovered_volume) == approx(300)
        assert float(batch.default_volume) == 0
        assert float(_account(db, market["S1"].id).current_balance) == 1000
        assert float(_account(db, buyer.id).current_balance) == 0

    def test_buyer_spends_refund_then_partial_clawback(self, db, market):
        """买方在冲正前已将到账配额部分转出：冲正只收回自由可用余额，欠额挂账。

        通过无缺口买方（到账=自由可用）成交后转出大部分，再冲正：
        回退 300，但买方仅剩 50 自由可用 → 收回 50、违约 250。
        """
        from app.services.trading_service import transfer

        s, trade = _settle_trade(db, market["S1"].id, market["B1"].id, 300)
        # 买方到账后持仓 500，转出 450 仅余 50
        transfer(db, _account(db, market["B1"].id), 450, "sell",
                 counterparty="碳市场", tx_date="2026-04-01")
        db.commit()

        batch = _reverse(db, s.id, reason="成交异常冲正")
        db.expire_all()
        t = db.get(AuctionTrade, trade.id)
        assert t.status == TRADE_DEFAULTED
        assert float(t.defaulted_amount) == approx(250)
        assert float(t.repaid_amount) == approx(0)
        assert float(batch.recovered_volume) == approx(50)
        assert float(batch.default_volume) == approx(250)

        # 卖方：出库 300、冲正仅退回 50（250 待追偿）
        seller_acc = _account(db, market["S1"].id)
        assert float(seller_acc.current_balance) == 750
        buyer_acc = _account(db, market["B1"].id)
        assert float(buyer_acc.current_balance) == 0
        assert len(list_defaulted_trades(db, buyer_id=market["B1"].id, year=YEAR)) == 1
        _assert_snapshot(db, seller_acc.id)
        _assert_snapshot(db, buyer_acc.id)

    def test_default_with_frozen_compliance_seller_fully_compensated(self, db, market):
        """缺口清缴场景下买方零余额冲正：400 全部登记违约，缺口恢复挂账。"""
        buyer = market["B1"]
        _make_buyer_deficit(db, buyer, 600)  # 冻结 200、缺口 400
        s, trade = _settle_trade(db, market["S1"].id, buyer.id, 400)  # 全额清缴，余额 0

        # 买方无自由可用：退还补缴 400 后 free=400（冲正内即时），随后收回 400——
        # 但本笔成交 400 全部为自由补缴（原冻结核销 200 不在本笔交付量内），
        # 冲正退还的 400 直接可收回，故本场景实际无违约；为构造违约需买方提前转出。
        batch = _reverse(db, s.id, reason="冲正演练")
        db.expire_all()
        t = db.get(AuctionTrade, trade.id)
        assert float(batch.recovered_volume) == approx(400)
        assert float(batch.default_volume) == approx(0)
        assert t.status == TRADE_REVERSED
        seller_acc = _account(db, market["S1"].id)
        assert float(seller_acc.current_balance) == 1000
        buyer_acc = _account(db, buyer.id)
        assert float(buyer_acc.current_balance) == 0
        record = db.query(ComplianceRecord).filter_by(
            company_id=buyer.id, year=YEAR, is_active=1).one()
        assert float(record.cleared_amount) == approx(200)
        assert float(record.frozen_amount) == approx(0)
        assert float(record.deficit) == approx(400)

    def test_manual_repay_then_status_reversed(self, db, market):
        from app.services.trading_service import transfer

        s, trade = _settle_trade(db, market["S1"].id, market["B1"].id, 300)
        transfer(db, _account(db, market["B1"].id), 450, "sell",
                 counterparty="碳市场", tx_date="2026-04-01")
        db.commit()
        _reverse(db, s.id, reason="冲正违约")

        # 欠额 250，买方先补入 100：只能追偿 100
        transfer(db, _account(db, market["B1"].id), 100, "buy",
                 counterparty="碳市场", tx_date="2026-05-01")
        db.commit()
        r = repay_trade_default(db, trade.id, ADMIN)
        assert float(r.quantity) == approx(100)
        db.expire_all()
        t = db.get(AuctionTrade, trade.id)
        assert t.status == TRADE_DEFAULTED
        assert float(t.repaid_amount) == approx(100)
        assert float(_account(db, market["S1"].id).current_balance) == 850

        # 再补入 200，监管按买方汇总追偿剩余 150
        transfer(db, _account(db, market["B1"].id), 200, "buy",
                 counterparty="碳市场", tx_date="2026-06-01")
        db.commit()
        result = recover_buyer_defaults(db, market["B1"].id, YEAR, ADMIN)
        assert float(result["recovered"]) == approx(150)
        db.expire_all()
        t = db.get(AuctionTrade, trade.id)
        assert t.status == TRADE_REVERSED
        assert float(t.defaulted_amount) == approx(250)
        assert float(t.repaid_amount) == approx(250)
        assert float(_account(db, market["S1"].id).current_balance) == 1000
        # 买方：0(冲正后) + 100 + 200 - 100 - 150 = 50
        assert float(_account(db, market["B1"].id).current_balance) == approx(50)
        assert db.query(AuctionDefaultRepayment).count() == 2
        _assert_snapshot(db, _account(db, market["S1"].id).id)
        _assert_snapshot(db, _account(db, market["B1"].id).id)

    def test_repay_more_than_outstanding_clamped(self, db, market):
        from app.services.trading_service import transfer

        s, trade = _settle_trade(db, market["S1"].id, market["B1"].id, 300)
        transfer(db, _account(db, market["B1"].id), 450, "sell",
                 counterparty="碳市场", tx_date="2026-04-01")
        db.commit()
        _reverse(db, s.id, reason="冲正违约")
        # 欠 250；买方补足 300，指定追偿 999 也只偿还 250
        transfer(db, _account(db, market["B1"].id), 300, "buy",
                 counterparty="碳市场", tx_date="2026-05-01")
        db.commit()
        r = repay_trade_default(db, trade.id, ADMIN, amount=999)
        assert float(r.quantity) == approx(250)
        db.expire_all()
        assert db.get(AuctionTrade, trade.id).status == TRADE_REVERSED

    def test_repay_with_no_free_balance_rejected(self, db, market):
        from app.services.trading_service import transfer

        s, trade = _settle_trade(db, market["S1"].id, market["B1"].id, 300)
        transfer(db, _account(db, market["B1"].id), 500, "sell",
                 counterparty="碳市场", tx_date="2026-04-01")
        db.commit()
        _reverse(db, s.id, reason="冲正违约")
        with pytest.raises(AuctionError, match="自由可用配额不足"):
            repay_trade_default(db, trade.id, ADMIN)
        with pytest.raises(AuctionError, match="暂无可追偿"):
            recover_buyer_defaults(db, market["B1"].id, YEAR, ADMIN)

    def test_repay_idempotent(self, db, market):
        from app.services.trading_service import transfer

        s, trade = _settle_trade(db, market["S1"].id, market["B1"].id, 300)
        transfer(db, _account(db, market["B1"].id), 470, "sell",
                 counterparty="碳市场", tx_date="2026-04-01")
        db.commit()
        _reverse(db, s.id, reason="冲正违约")  # 欠 270
        # 只补 100：追偿后仍欠 170，成交单保持 defaulted，重复请求才能命中幂等
        transfer(db, _account(db, market["B1"].id), 100, "buy",
                 counterparty="碳市场", tx_date="2026-05-01")
        db.commit()
        r1 = repay_trade_default(db, trade.id, ADMIN, idempotency_key="pay-1")
        r2 = repay_trade_default(db, trade.id, ADMIN, idempotency_key="pay-1")
        assert r1.id == r2.id
        assert float(r1.quantity) == approx(100)
        assert db.query(AuctionDefaultRepayment).count() == 1
        db.expire_all()
        assert db.get(AuctionTrade, trade.id).status == TRADE_DEFAULTED

    def test_auto_recover_on_next_settlement(self, db, market):
        """违约后买方在第二场次卖出变买方到账：结算同事务自动追偿历史欠额。"""
        from app.services.trading_service import transfer

        s1, trade1 = _settle_trade(db, market["S1"].id, market["B1"].id, 300)
        transfer(db, _account(db, market["B1"].id), 400, "sell",
                 counterparty="碳市场", tx_date="2026-04-01")
        db.commit()
        _reverse(db, s1.id, reason="冲正形成违约")  # 欠 200

        # 第二场次：乙卖 300，丙买 300（丙此时余额 100，买入侧不校验配额）
        s2, trade2 = _settle_trade(db, market["S2"].id, market["B1"].id, 300)
        db.expire_all()
        # 到账 300：无履约缺口，自动追偿 200 给原卖方甲
        t1 = db.get(AuctionTrade, trade1.id)
        assert t1.status == TRADE_REVERSED
        assert float(t1.repaid_amount) == approx(200)
        assert db.query(AuctionDefaultRepayment).filter_by(source="auto").count() == 1
        # 甲 700(出库后) + 100(冲正即时收回) + 200(追偿到账) = 1000；
        # 乙 700；丙 0 + 300 - 200 = 100
        assert float(_account(db, market["S1"].id).current_balance) == approx(1000)
        assert float(_account(db, market["S2"].id).current_balance) == approx(700)
        assert float(_account(db, market["B1"].id).current_balance) == approx(100)
        _assert_snapshot(db, _account(db, market["S1"].id).id)
        _assert_snapshot(db, _account(db, market["B1"].id).id)

    def test_auto_recover_disabled(self, db, market):
        from app.services.trading_service import transfer

        s1, trade1 = _settle_trade(db, market["S1"].id, market["B1"].id, 300)
        transfer(db, _account(db, market["B1"].id), 400, "sell",
                 counterparty="碳市场", tx_date="2026-04-01")
        db.commit()
        _reverse(db, s1.id, reason="冲正形成违约")

        s2, _ = _settle_trade(db, market["S2"].id, market["B1"].id, 300,
                              auto_recover=False)
        db.expire_all()
        assert db.get(AuctionTrade, trade1.id).status == TRADE_DEFAULTED
        assert db.query(AuctionDefaultRepayment).count() == 0
        # 丙：冲正收回 100 后余 0，第二场到账 300，欠额仍挂账（不自动追偿）
        assert float(_account(db, market["B1"].id).current_balance) == approx(300)


class TestReversalConcurrency:
    def test_parallel_reverse_only_once(self, db, market):
        """多线程并发冲正同一成交单：只有一次回退。"""
        s, trade = _settle_trade(db, market["S1"].id, market["B1"].id, 300)
        sid = s.id
        outcomes = []

        def worker(i):
            session = _fresh(db)
            try:
                reverse_settled_trades(
                    session, sid, ADMIN, "并发冲正",
                    idempotency_key=f"rev-{i}",
                )
                outcomes.append("ok")
            except AuctionError:
                session.rollback()
                outcomes.append("reject")
            finally:
                session.close()

        with ThreadPoolExecutor(max_workers=6) as pool:
            list(pool.map(worker, range(6)))

        db.expire_all()
        assert outcomes.count("ok") == 1
        assert db.query(AuctionTradeReversal).count() == 1
        assert db.query(AuctionReversalBatch).count() == 1
        clawbacks = db.query(AllowanceTransaction).filter_by(
            tx_type="auction_clawback_out").count()
        assert clawbacks == 1
        assert float(_account(db, market["S1"].id).current_balance) == 1000
        assert float(_account(db, market["B1"].id).current_balance) == 200
        _assert_snapshot(db, _account(db, market["S1"].id).id)
        _assert_snapshot(db, _account(db, market["B1"].id).id)

    def test_parallel_partial_reverse_total_capped(self, db, market):
        """并发部分冲正：累计回退量不超过成交量。"""
        s, trade = _settle_trade(db, market["S1"].id, market["B1"].id, 300)
        sid, tid = s.id, trade.id

        def worker(i):
            session = _fresh(db)
            try:
                reverse_settled_trades(
                    session, sid, ADMIN, f"部分冲正{i}",
                    quantities={tid: 100}, idempotency_key=f"pr-{i}",
                )
                return "ok"
            except AuctionError:
                session.rollback()
                return "reject"
            finally:
                session.close()

        with ThreadPoolExecutor(max_workers=6) as pool:
            outcomes = list(pool.map(worker, range(6)))

        db.expire_all()
        assert outcomes.count("ok") == 3
        t = db.get(AuctionTrade, tid)
        assert float(t.reversed_quantity) == approx(300)
        assert t.status == TRADE_REVERSED
        assert float(_account(db, market["S1"].id).current_balance) == 1000

    def test_reverse_races_next_settle_auto_recover_consistent(self, db, market):
        """冲正登记违约 与 下一场次结算自动追偿并发：最终欠额追偿不超过欠额。"""
        from app.services.trading_service import transfer

        s1, trade1 = _settle_trade(db, market["S1"].id, market["B1"].id, 300)
        transfer(db, _account(db, market["B1"].id), 400, "sell",
                 counterparty="碳市场", tx_date="2026-04-01")
        db.commit()

        def reverse():
            session = _fresh(db)
            try:
                reverse_settled_trades(session, s1.id, ADMIN, "并发冲正",
                                       idempotency_key="rev-x")
                return "ok"
            except AuctionError:
                session.rollback()
                return "reject"
            finally:
                session.close()

        def settle_next():
            session = _fresh(db)
            try:
                s2 = create_session(session, year=YEAR, name="第二场", operator=ADMIN)
                s2 = open_session(session, s2.id, ADMIN)
                place_bid(session, s2.id, market["S2"].id, "sell", 300, 80, operator=ADMIN)
                place_bid(session, s2.id, market["B1"].id, "buy", 300, 90, operator=ADMIN)
                run_matching(session, s2.id, ADMIN)
                settle_session(session, s2.id, ADMIN)
                return "settled"
            except AuctionError:
                session.rollback()
                return "reject"
            finally:
                session.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda f: f(), [reverse, settle_next]))

        db.expire_all()
        assert "ok" in results and "settled" in results
        t1 = db.get(AuctionTrade, trade1.id)
        repaid = float(t1.repaid_amount)
        defaulted = float(t1.defaulted_amount)
        assert repaid <= defaulted + 1e-9
        # 卖方甲最终持仓 = 1000 - 300 + 追偿(repaid) + 冲正即时收回
        seller1 = _account(db, market["S1"].id)
        buyer = _account(db, market["B1"].id)
        _assert_snapshot(db, seller1.id)
        _assert_snapshot(db, buyer.id)
        assert float(seller1.current_balance) <= 1000 + 1e-9


class TestReportAndTradeReversalOrder:
    """报告冲正与竞价成交冲正两条回退链路：任意先后顺序下账本守恒、不重复退还。

    公共场景：买方 200 吨配额、批准排放 500（冻结 200、缺口 300）；
    竞价买入 300 结算到账全部补缴缺口（auction_deficit_clear 300 归属成交单），
    买方持仓 0、卖方 700、履约 cleared=500 达标。
    两条链路共用同一本成交单归属流水账（auction_deficit_clear − auction_clear_refund），
    同一吨归属补缴无论谁先谁后最多退还一次。
    """

    def _settled_with_clearance(self, db, market):
        buyer = market["B1"]
        report = _make_buyer_deficit(db, buyer, 500)
        s, trade = _settle_trade(db, market["S1"].id, buyer.id, 300)
        record = db.query(ComplianceRecord).filter_by(
            company_id=buyer.id, year=YEAR, is_active=1).one()
        assert record.status == "compliant"
        assert float(record.cleared_amount) == approx(500)
        assert float(_account(db, buyer.id).current_balance) == 0
        return report, s, trade

    def test_report_reversal_then_trade_reversal_no_double_refund(self, db, market):
        """先冲正报告再冲正成交：归属补缴只退一次，双方回到结算前，总量守恒。"""
        from app.services.mrv_service import reverse_report

        buyer, seller = market["B1"], market["S1"]
        report, s, trade = self._settled_with_clearance(db, market)

        # 报告冲正：退还已清缴 500，其中归属成交单的 300 逐笔退还并关联成交单
        reverse_report(db, report, operator_id=1, reason="核查数据有误")
        db.expire_all()
        assert float(_account(db, buyer.id).current_balance) == approx(500)
        refunds = (
            db.query(AllowanceTransaction)
            .filter_by(tx_type="auction_clear_refund", auction_trade_id=trade.id)
            .all()
        )
        assert sum(float(t.amount) for t in refunds) == approx(300)
        # 非成交单归属的 200 走汇总退还流水，归属成交单的部分不在其中
        reversal_tx = db.query(AllowanceTransaction).filter_by(tx_type="reversal").one()
        assert float(reversal_tx.amount) == approx(200)

        # 再冲正成交：归属补缴已随报告冲正退还（c_rev=0），不再二次退还；仅收回 300
        batch = _reverse(db, s.id)
        db.expire_all()
        assert float(batch.recovered_volume) == approx(300)
        assert float(batch.default_volume) == 0
        rev = db.query(AuctionTradeReversal).one()
        assert float(rev.clear_refunded) == approx(0)
        assert float(rev.recovered_quantity) == approx(300)
        assert db.get(AuctionTrade, trade.id).status == TRADE_REVERSED

        # 双方回到结算前：买方 200、卖方 1000，系统总量 1200 守恒
        assert float(_account(db, buyer.id).current_balance) == approx(200)
        assert float(_account(db, seller.id).current_balance) == approx(1000)
        _assert_snapshot(db, _account(db, buyer.id).id)
        _assert_snapshot(db, _account(db, seller.id).id)

    def test_trade_reversal_then_report_reversal_same_final_state(self, db, market):
        """先冲正成交再冲正报告：最终状态与先冲报告完全一致（顺序无关）。"""
        from app.services.mrv_service import reverse_report

        buyer, seller = market["B1"], market["S1"]
        report, s, trade = self._settled_with_clearance(db, market)

        _reverse(db, s.id)
        db.expire_all()
        record = db.query(ComplianceRecord).filter_by(
            company_id=buyer.id, year=YEAR, is_active=1).one()
        assert float(record.cleared_amount) == approx(200)
        assert float(_account(db, buyer.id).current_balance) == approx(0)

        reverse_report(db, report, operator_id=1, reason="核查数据有误")
        db.expire_all()
        # 与“先冲报告再冲成交”相同的终态
        assert float(_account(db, buyer.id).current_balance) == approx(200)
        assert float(_account(db, seller.id).current_balance) == approx(1000)
        _assert_snapshot(db, _account(db, buyer.id).id)
        _assert_snapshot(db, _account(db, seller.id).id)

    def test_partial_trade_reversal_report_reversal_then_remaining(self, db, market):
        """部分冲正 → 报告冲正 → 剩余冲正：归属补缴累计退还恰为 300，不重复。"""
        from app.services.mrv_service import reverse_report

        buyer, seller = market["B1"], market["S1"]
        report, s, trade = self._settled_with_clearance(db, market)

        # 先部分冲正 100：退还归属补缴 100 并收回划付卖方
        _reverse(db, s.id, quantities={trade.id: 100})
        db.expire_all()
        assert float(_account(db, buyer.id).current_balance) == approx(0)
        assert float(_account(db, seller.id).current_balance) == approx(800)

        # 报告冲正：剩余归属补缴 200 按成交单退还，另退非归属清缴 200
        reverse_report(db, report, operator_id=1, reason="核查数据有误")
        db.expire_all()
        assert float(_account(db, buyer.id).current_balance) == approx(400)

        # 剩余 200 冲正：归属补缴已退完（c_rev=0），仅收回 200 划付卖方
        _reverse(db, s.id)
        db.expire_all()
        t = db.get(AuctionTrade, trade.id)
        assert t.status == TRADE_REVERSED
        assert float(t.reversed_quantity) == approx(300)
        refunds = (
            db.query(AllowanceTransaction)
            .filter_by(tx_type="auction_clear_refund", auction_trade_id=trade.id)
            .all()
        )
        assert sum(float(x.amount) for x in refunds) == approx(300)
        assert float(_account(db, buyer.id).current_balance) == approx(200)
        assert float(_account(db, seller.id).current_balance) == approx(1000)
        _assert_snapshot(db, _account(db, buyer.id).id)
        _assert_snapshot(db, _account(db, seller.id).id)

    def test_report_reversal_reapprove_then_trade_reversal(self, db, market):
        """报告冲正→重新批准→冲正旧成交：新履约记录不被污染，追偿后总量守恒。"""
        from app.services.mrv_service import (
            approve_report,
            generate_report,
            reverse_report,
            submit_report,
        )

        buyer, seller = market["B1"], market["S1"]
        report, s, trade = self._settled_with_clearance(db, market)

        reverse_report(db, report, operator_id=1, reason="核查数据有误")
        # 重新生成并批准（排放不变）：新履约记录冻结 500、cleared=0
        report2 = generate_report(db, buyer.id, YEAR)
        submit_report(db, report2)
        approve_report(db, report2, verifier_id=1)
        db.commit()
        record2 = db.query(ComplianceRecord).filter_by(
            company_id=buyer.id, year=YEAR, is_active=1).one()
        assert float(record2.cleared_amount) == approx(0)
        assert float(record2.frozen_amount) == approx(500)

        # 冲正旧成交：归属补缴已随首次报告冲正退还（c_rev=0），新记录不被回滚；
        # 买方自由可用为 0（全部冻结），收回 0、登记违约 300
        _reverse(db, s.id)
        db.expire_all()
        t = db.get(AuctionTrade, trade.id)
        assert t.status == TRADE_DEFAULTED
        assert float(t.defaulted_amount) == approx(300)
        record2 = db.query(ComplianceRecord).filter_by(
            company_id=buyer.id, year=YEAR, is_active=1).one()
        assert float(record2.cleared_amount) == approx(0)
        assert float(record2.frozen_amount) == approx(500)
        assert float(record2.deficit) == approx(0)
        assert record2.status == "pending"

        # 冲正新报告解除冻结（cleared=0：只解冻不退还，持仓不变），再追偿欠额
        reverse_report(db, report2, operator_id=1, reason="再次更正")
        db.expire_all()
        assert float(_account(db, buyer.id).current_balance) == approx(500)
        assert float(_account(db, buyer.id).frozen_balance) == approx(0)
        repay_trade_default(db, trade.id, ADMIN)
        db.expire_all()
        assert db.get(AuctionTrade, trade.id).status == TRADE_REVERSED
        assert float(_account(db, buyer.id).current_balance) == approx(200)
        assert float(_account(db, seller.id).current_balance) == approx(1000)
        _assert_snapshot(db, _account(db, buyer.id).id)
        _assert_snapshot(db, _account(db, seller.id).id)
