"""企业间交易订单测试：双方确认、撤销、交割与“交易占用 × 履约冻结”冲突。

覆盖：
- 挂单 → 单方/双方确认 → 交割的完整状态机，双方账户余额与流水同步；
- 交割前任一方撤销：confirmed 订单释放卖方交易占用，pending 订单无副作用；
- 已确认订单占用卖方可用配额，期间卖方不能超卖、不能重复占用；
- 履约冻结与交易占用互不可挤占（批准冻结 / 清缴补扣均不能挪用占用配额）；
- 卖方可用不足时双方确认被拒绝且无副作用，撤销后可重新确认；
- 买方不存在年度账户时禁止挂单；非参与方无权确认/撤销/交割；
- 幂等建单；重复交割/重复撤销幂等；非法状态流转被拒绝；
- 多线程并发：重复交割只入账一次、交割与撤销竞争不产生脏账、
  同一卖方并发确认多张订单总占用不超过可用余额；
- 流水快照链（持仓/冻结/占用）逐笔可推算。
"""

from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from app.core.database import Base
from app.models import (
    ActivityData,
    AllowanceAccount,
    AllowanceTransaction,
    CalculationMethod,
    Company,
    EmissionFactor,
    EmissionScope,
    TradeOrder,
)
from app.services.mrv_service import approve_report, generate_report, submit_report
from app.services.quota_service import allocate_quota, clear_emission
from app.services.trade_order_service import (
    CANCELLED,
    CONFIRMED,
    DELIVERED,
    PENDING,
    TradeOrderError,
    cancel_order,
    confirm_order,
    create_order,
    deliver_order,
)


def approx(value, rel=1e-6):
    return pytest.approx(float(value), rel=rel)


@pytest.fixture()
def db(tmp_path):
    """文件型临时库：多线程共享同一份数据。"""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'trade_orders.db'}",
        connect_args={"check_same_thread": False, "timeout": 30},
    )

    @event.listens_for(engine, "connect")
    def _busy_timeout(dbapi_conn, _rec):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA busy_timeout=30000")
        cur.close()

    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()
    engine.dispose()


@pytest.fixture()
def two_companies(db):
    """两家企业：卖方 1000 吨、买方 400 吨（2025 年度账户）。"""
    seller = Company(code="SELL-001", name="卖方企业", industry="电力", region="华东")
    buyer = Company(code="BUY-001", name="买方企业", industry="水泥", region="华北")
    db.add_all([seller, buyer])
    db.flush()

    # 核算边界与外购电力因子（0.5703 tCO2/MWh），供冻结/清缴冲突测试构造排放量
    scope = EmissionScope(company_id=seller.id, scope="2", category="外购电力", name="厂区用电")
    db.add(scope)
    db.add(
        CalculationMethod(
            method_code="ELEC", name="外购电力排放因子法", scope="2", formula_type="activity_factor"
        )
    )
    db.add(
        EmissionFactor(
            factor_code="ELEC-GRID", name="外购电力", scope="2", unit="tCO2/MWh", value=0.5703,
            source="电网因子", valid_from="2024-01-01", valid_to="2025-12-31",
        )
    )

    allocate_quota(db, seller.id, 2025, baseline=1000, allocation_amount=1000)
    allocate_quota(db, buyer.id, 2025, baseline=400, allocation_amount=400)
    db.commit()
    db.expire_all()
    return {"seller": seller, "buyer": buyer}


def _accounts(db, ctx):
    seller_acc = db.query(AllowanceAccount).filter_by(company_id=ctx["seller"].id, year=2025).one()
    buyer_acc = db.query(AllowanceAccount).filter_by(company_id=ctx["buyer"].id, year=2025).one()
    return seller_acc, buyer_acc


def _fresh_session(db):
    return Session(bind=db.bind)


def _assert_snapshot(db, account_id):
    """流水不变量：持仓/冻结/占用三类快照逐笔可推算，末笔等于账户现值。"""
    txs = (
        db.query(AllowanceTransaction)
        .filter(AllowanceTransaction.account_id == account_id)
        .order_by(AllowanceTransaction.id.asc())
        .all()
    )
    current_signs = {
        "allocation": 1, "buy": 1, "transfer_in": 1, "reversal": 1,
        "trade_deliver_in": 1,
        "sell": -1, "transfer_out": -1, "offset": -1, "clear": -1,
        "frozen_clear": -1, "trade_deliver_out": -1,
        "freeze": 0, "trade_reserve": 0, "trade_release": 0,
    }
    frozen_signs = {"freeze": 1, "frozen_clear": -1, "reversal_unfreeze": -1}
    reserved_signs = {"trade_reserve": 1, "trade_release": -1, "trade_deliver_out": -1}
    exp_c = exp_f = exp_r = 0.0
    for tx in txs:
        exp_c = round(exp_c + current_signs.get(tx.tx_type, 0) * float(tx.amount), 4)
        exp_f = round(exp_f + frozen_signs.get(tx.tx_type, 0) * float(tx.amount), 4)
        exp_r = round(exp_r + reserved_signs.get(tx.tx_type, 0) * float(tx.amount), 4)
        assert float(tx.balance_after) == approx(exp_c), f"流水#{tx.id} 持仓快照不符"
        assert float(tx.frozen_after or 0) == approx(exp_f), f"流水#{tx.id} 冻结快照不符"
        assert float(tx.reserved_after or 0) == approx(exp_r), f"流水#{tx.id} 占用快照不符"
    account = db.get(AllowanceAccount, account_id)
    assert float(account.current_balance) == approx(exp_c)
    assert float(account.frozen_balance) == approx(exp_f)
    assert float(account.reserved_balance) == approx(exp_r)
    assert float(account.frozen_balance) + float(account.reserved_balance) <= float(account.current_balance) + 1e-9
    return exp_c, exp_f, exp_r


class TestOrderLifecycle:
    def test_create_pending_no_occupancy(self, db, two_companies):
        """卖方挂单：状态 pending、仅卖方确认，不占用任何配额。"""
        ctx = two_companies
        order = create_order(db, ctx["seller"].id, ctx["buyer"].id, 2025, 300, price=80)
        assert order.status == PENDING
        assert bool(order.seller_confirmed) is True
        assert bool(order.buyer_confirmed) is False
        assert order.order_no.startswith("TO2025")
        seller_acc, buyer_acc = _accounts(db, ctx)
        assert float(seller_acc.reserved_balance) == 0
        assert float(seller_acc.current_balance) == 1000
        # 挂单不产生流水
        assert db.query(AllowanceTransaction).filter(
            AllowanceTransaction.tx_type.like("trade_%")
        ).count() == 0

    def test_buyer_initiated_order(self, db, two_companies):
        """买方求购：发起方为买方时买方默认确认，等待卖方确认。"""
        ctx = two_companies
        order = create_order(db, ctx["seller"].id, ctx["buyer"].id, 2025, 300, initiator="buyer")
        assert bool(order.buyer_confirmed) is True
        assert bool(order.seller_confirmed) is False
        order = confirm_order(db, order.id, ctx["seller"].id)
        assert order.status == CONFIRMED

    def test_both_confirm_occupies_seller_allowance(self, db, two_companies):
        """双方确认：卖方自由可用配额转为交易占用，持仓与冻结不变。"""
        ctx = two_companies
        order = create_order(db, ctx["seller"].id, ctx["buyer"].id, 2025, 300, price=80)
        order = confirm_order(db, order.id, ctx["buyer"].id)
        assert order.status == CONFIRMED
        assert order.confirmed_at is not None

        seller_acc, buyer_acc = _accounts(db, ctx)
        assert float(seller_acc.current_balance) == 1000
        assert float(seller_acc.frozen_balance) == 0
        assert float(seller_acc.reserved_balance) == 300
        # 买方在确认阶段不受影响
        assert float(buyer_acc.current_balance) == 400
        assert float(buyer_acc.reserved_balance) == 0

        reserve_tx = db.query(AllowanceTransaction).filter_by(tx_type="trade_reserve").one()
        assert float(reserve_tx.amount) == 300
        assert float(reserve_tx.balance_after) == 1000
        assert float(reserve_tx.reserved_after) == 300
        assert reserve_tx.trade_order_id == order.id
        assert float(reserve_tx.price) == 80
        _assert_snapshot(db, seller_acc.id)

    def test_confirm_flags_persist_across_sessions(self, db, two_companies):
        """回归（autoflush=False）：双方确认后两个确认标志必须独立持久化。

        历史缺陷：买方确认标志先标记为脏，随后账本 UPDATE 的 expire_all
        在 flush 前把待写入属性丢弃，导致状态已 confirmed 但标志仍是单方确认。
        """
        ctx = two_companies
        sid, bid = ctx["seller"].id, ctx["buyer"].id
        oid = create_order(db, sid, bid, 2025, 300).id
        db.commit()

        # 用独立会话执行买方确认，模拟真实 HTTP 请求
        s1 = _fresh_session(db)
        try:
            confirm_order(s1, oid, bid)
        finally:
            s1.close()

        s2 = _fresh_session(db)
        try:
            order = s2.get(TradeOrder, oid)
            assert order.status == CONFIRMED
            assert int(order.seller_confirmed) == 1
            assert int(order.buyer_confirmed) == 1
        finally:
            s2.close()

    def test_deliver_settles_both_accounts_and_ledger(self, db, two_companies):
        """交割：卖方持仓/占用同减，买方持仓同增，双方各写交割流水。"""
        ctx = two_companies
        order = create_order(db, ctx["seller"].id, ctx["buyer"].id, 2025, 300, price=80)
        confirm_order(db, order.id, ctx["buyer"].id)
        order = deliver_order(db, order.id, ctx["seller"].id)
        assert order.status == DELIVERED
        assert order.delivered_at is not None

        db.expire_all()
        seller_acc, buyer_acc = _accounts(db, ctx)
        assert float(seller_acc.current_balance) == 700
        assert float(seller_acc.reserved_balance) == 0
        assert float(buyer_acc.current_balance) == 700

        out_tx = db.query(AllowanceTransaction).filter_by(tx_type="trade_deliver_out").one()
        in_tx = db.query(AllowanceTransaction).filter_by(tx_type="trade_deliver_in").one()
        assert out_tx.account_id == seller_acc.id
        assert in_tx.account_id == buyer_acc.id
        assert float(out_tx.balance_after) == 700
        assert float(out_tx.reserved_after) == 0
        assert float(in_tx.balance_after) == 700
        assert out_tx.trade_order_id == in_tx.trade_order_id == order.id
        assert out_tx.counterparty == "买方企业"
        assert in_tx.counterparty == "卖方企业"
        _assert_snapshot(db, seller_acc.id)
        _assert_snapshot(db, buyer_acc.id)

    def test_cancel_pending_order_has_no_ledger_side_effect(self, db, two_companies):
        """挂单阶段撤销：不涉及占用，无交易流水。"""
        ctx = two_companies
        order = create_order(db, ctx["seller"].id, ctx["buyer"].id, 2025, 300)
        order = cancel_order(db, order.id, ctx["buyer"].id, reason="价格不合适")
        assert order.status == CANCELLED
        assert order.cancelled_by == ctx["buyer"].id
        assert order.cancel_reason == "价格不合适"
        assert db.query(AllowanceTransaction).filter(
            AllowanceTransaction.tx_type.like("trade_%")
        ).count() == 0
        seller_acc, _ = _accounts(db, ctx)
        assert float(seller_acc.reserved_balance) == 0

    def test_cancel_confirmed_order_releases_reservation(self, db, two_companies):
        """确认后撤销：卖方占用配额释放回可用，持仓不变，留释放流水。"""
        ctx = two_companies
        order = create_order(db, ctx["seller"].id, ctx["buyer"].id, 2025, 300)
        confirm_order(db, order.id, ctx["buyer"].id)
        order = cancel_order(db, order.id, ctx["seller"].id, reason="终止交易")
        assert order.status == CANCELLED

        db.expire_all()
        seller_acc, _ = _accounts(db, ctx)
        assert float(seller_acc.current_balance) == 1000
        assert float(seller_acc.reserved_balance) == 0
        release_tx = db.query(AllowanceTransaction).filter_by(tx_type="trade_release").one()
        assert float(release_tx.amount) == 300
        assert float(release_tx.reserved_after) == 0
        _assert_snapshot(db, seller_acc.id)

    def test_reconfirm_after_cancel(self, db, two_companies):
        """挂单被对方撤销后可重新挂单交易，卖方配额仍可被新订单占用/交割。"""
        ctx = two_companies
        order = create_order(db, ctx["seller"].id, ctx["buyer"].id, 2025, 300)
        cancel_order(db, order.id, ctx["buyer"].id)
        new_order = create_order(db, ctx["seller"].id, ctx["buyer"].id, 2025, 200)
        confirm_order(db, new_order.id, ctx["buyer"].id)
        new_order = deliver_order(db, new_order.id, ctx["seller"].id)
        assert new_order.status == DELIVERED
        seller_acc, buyer_acc = _accounts(db, ctx)
        assert float(seller_acc.current_balance) == 800
        assert float(buyer_acc.current_balance) == 600


class TestOrderRules:
    def test_idempotent_create(self, db, two_companies):
        """相同幂等键重复挂单返回同一订单。"""
        ctx = two_companies
        first = create_order(db, ctx["seller"].id, ctx["buyer"].id, 2025, 300, idempotency_key="ord-1")
        second = create_order(db, ctx["seller"].id, ctx["buyer"].id, 2025, 999, idempotency_key="ord-1")
        assert first.id == second.id
        assert float(second.amount) == 300
        assert db.query(TradeOrder).count() == 1

    def test_same_company_rejected(self, db, two_companies):
        ctx = two_companies
        with pytest.raises(TradeOrderError, match="同一企业"):
            create_order(db, ctx["seller"].id, ctx["seller"].id, 2025, 100)

    def test_invalid_amount_and_price(self, db, two_companies):
        ctx = two_companies
        with pytest.raises(Exception):
            create_order(db, ctx["seller"].id, ctx["buyer"].id, 2025, 0)
        with pytest.raises(TradeOrderError, match="正数"):
            create_order(db, ctx["seller"].id, ctx["buyer"].id, 2025, -10)
        with pytest.raises(TradeOrderError, match="单价"):
            create_order(db, ctx["seller"].id, ctx["buyer"].id, 2025, 100, price=-1)

    def test_missing_buyer_account_rejected(self, db, two_companies):
        """买方尚无该年度账户时不能挂单，避免交割时无法到账。"""
        ctx = two_companies
        third = Company(code="C-003", name="无账户企业", industry="化工", region="华南")
        db.add(third)
        db.commit()
        with pytest.raises(TradeOrderError, match="配额账户不存在"):
            create_order(db, ctx["seller"].id, third.id, 2025, 100)

    def test_non_party_cannot_operate(self, db, two_companies):
        ctx = two_companies
        third = Company(code="C-003", name="第三方企业", industry="化工", region="华南")
        db.add(third)
        db.commit()
        order = create_order(db, ctx["seller"].id, ctx["buyer"].id, 2025, 300)
        with pytest.raises(TradeOrderError, match="无权操作"):
            confirm_order(db, order.id, third.id)
        with pytest.raises(TradeOrderError, match="无权操作"):
            cancel_order(db, order.id, third.id)
        with pytest.raises(TradeOrderError, match="无权操作"):
            deliver_order(db, order.id, third.id)

    def test_deliver_requires_both_confirmation(self, db, two_companies):
        """仅单方确认的挂单不能交割。"""
        ctx = two_companies
        order = create_order(db, ctx["seller"].id, ctx["buyer"].id, 2025, 300)
        with pytest.raises(TradeOrderError, match="双方确认"):
            deliver_order(db, order.id, ctx["seller"].id)

    def test_cancel_delivered_is_idempotent_no_release(self, db, two_companies):
        """已交割订单的撤销调用幂等返回，不释放任何配额、不写释放流水。"""
        ctx = two_companies
        order = create_order(db, ctx["seller"].id, ctx["buyer"].id, 2025, 300)
        confirm_order(db, order.id, ctx["buyer"].id)
        deliver_order(db, order.id, ctx["seller"].id)
        again = cancel_order(db, order.id, ctx["buyer"].id, reason="试图撤销")
        assert again.status == DELIVERED
        assert db.query(AllowanceTransaction).filter_by(tx_type="trade_release").count() == 0

    def test_confirm_cancelled_or_delivered_rejected(self, db, two_companies):
        ctx = two_companies
        order = create_order(db, ctx["seller"].id, ctx["buyer"].id, 2025, 300)
        cancel_order(db, order.id, ctx["seller"].id)
        with pytest.raises(TradeOrderError, match="已交割或已撤销"):
            confirm_order(db, order.id, ctx["buyer"].id)

    def test_repeated_confirm_deliver_cancel_are_idempotent(self, db, two_companies):
        """已确认方重复确认、重复交割、重复撤销均为幂等，不重复记账。"""
        ctx = two_companies
        order = create_order(db, ctx["seller"].id, ctx["buyer"].id, 2025, 300)
        # 卖方（发起方）重复确认是空操作，订单仍 pending
        again = confirm_order(db, order.id, ctx["seller"].id)
        assert again.status == PENDING
        confirm_order(db, order.id, ctx["buyer"].id)
        # 买方重复确认不重复占用
        confirm_order(db, order.id, ctx["buyer"].id)
        seller_acc, _ = _accounts(db, ctx)
        assert float(seller_acc.reserved_balance) == 300

        d1 = deliver_order(db, order.id, ctx["buyer"].id)
        d2 = deliver_order(db, order.id, ctx["seller"].id)
        assert d1.id == d2.id == order.id
        assert db.query(AllowanceTransaction).filter_by(tx_type="trade_deliver_out").count() == 1
        assert db.query(AllowanceTransaction).filter_by(tx_type="trade_deliver_in").count() == 1

        # 已交割订单的重复撤销调用幂等返回订单本身（终态），不释放任何占用
        c1 = cancel_order(db, order.id, ctx["seller"].id)
        assert c1.status == DELIVERED
        assert db.query(AllowanceTransaction).filter_by(tx_type="trade_release").count() == 0


class TestReservationFreezeConflict:
    def test_reserved_allowance_cannot_be_resold(self, db, two_companies):
        """已确认订单占用的 300 吨不能再被普通卖出/划出（自由可用仅剩 700）。"""
        from app.services.trading_service import transfer

        ctx = two_companies
        order = create_order(db, ctx["seller"].id, ctx["buyer"].id, 2025, 300)
        confirm_order(db, order.id, ctx["buyer"].id)
        seller_acc, _ = _accounts(db, ctx)
        with pytest.raises(ValueError, match="可用配额"):
            transfer(db, seller_acc, 800, "sell", tx_date="2025-06-01")
        # 恰好用尽自由可用的 700 吨可以成交
        tx = transfer(db, seller_acc, 700, "sell", counterparty="交易所", tx_date="2025-06-01")
        assert float(tx.balance_after) == 300
        db.expire_all()
        seller_acc, _ = _accounts(db, ctx)
        # 持仓仅剩被占用的 300 吨，全部处于 reserved
        assert float(seller_acc.current_balance) == 300
        assert float(seller_acc.reserved_balance) == 300
        _assert_snapshot(db, seller_acc.id)

    def test_reserved_allowance_cannot_be_frozen_by_report_approval(self, db, two_companies):
        """报告批准冻结不能挪用交易占用：冻结只能覆盖自由可用部分，缺口留在履约记录。"""
        ctx = two_companies
        seller = ctx["seller"]

        # 先确认一张 300 吨订单（卖方持仓 1000，占用 300，自由可用 700）
        order = create_order(db, seller.id, ctx["buyer"].id, 2025, 300)
        confirm_order(db, order.id, ctx["buyer"].id)

        # 报告排放量 800 吨：只能冻结自由可用的 700 吨，占用的 300 吨不被挤占，
        # 履约记录形成 100 吨缺口
        db.add(
            ActivityData(
                company_id=seller.id,
                scope_id=_scope_id(db, seller),
                year=2025, period="monthly", activity_type="外购电力", unit="MWh",
                quantity=round(800 / 0.5703, 6), data_source="台账", verified=1,
            )
        )
        db.commit()
        from app.services.calculation_service import recalc_company_year

        recalc_company_year(db, seller.id, 2025)
        report = generate_report(db, seller.id, 2025)
        submit_report(db, report)
        approve_report(db, report, verifier_id=1)
        from app.models import ComplianceRecord

        record = (
            db.query(ComplianceRecord)
            .filter_by(company_id=seller.id, year=2025, is_active=1)
            .one()
        )

        seller_acc, _ = _accounts(db, ctx)
        assert float(seller_acc.current_balance) == 1000
        assert float(seller_acc.frozen_balance) == 700
        assert float(seller_acc.reserved_balance) == 300
        assert float(record.frozen_amount) == 700
        assert float(record.deficit) == approx(100)
        _assert_snapshot(db, seller_acc.id)

    def test_frozen_allowance_cannot_be_reserved_by_order(self, db, two_companies):
        """反向冲突：先冻结 600 吨履约配额后，可用只剩 400 吨，450 吨订单确认被拒。"""
        ctx = two_companies
        seller = ctx["seller"]
        db.add(
            ActivityData(
                company_id=seller.id, scope_id=_scope_id(db, seller), year=2025,
                period="monthly", activity_type="外购电力", unit="MWh",
                quantity=round(600 / 0.5703, 6), data_source="台账", verified=1,
            )
        )
        db.commit()
        from app.services.calculation_service import recalc_company_year

        recalc_company_year(db, seller.id, 2025)
        report = generate_report(db, seller.id, 2025)
        submit_report(db, report)
        approve_report(db, report, verifier_id=1)

        seller_acc, _ = _accounts(db, ctx)
        assert float(seller_acc.frozen_balance) == approx(600)

        order = create_order(db, seller.id, ctx["buyer"].id, 2025, 450)
        with pytest.raises(TradeOrderError, match="自由可用配额不足"):
            confirm_order(db, order.id, ctx["buyer"].id)

        # 确认失败无副作用：订单仍 pending、无占用流水、冻结/余额不变
        db.expire_all()
        seller_acc, _ = _accounts(db, ctx)
        assert float(seller_acc.reserved_balance) == 0
        assert float(seller_acc.frozen_balance) == approx(600)
        order = db.get(TradeOrder, order.id)
        assert order.status == PENDING
        assert db.query(AllowanceTransaction).filter(
            AllowanceTransaction.tx_type.like("trade_reserve%")
        ).count() == 0

        # 释放部分履约（冲正）后再确认可成功：直接撤销场景改为买方可用范围内更小订单
        small = create_order(db, seller.id, ctx["buyer"].id, 2025, 400)
        small = confirm_order(db, small.id, ctx["buyer"].id)
        assert small.status == CONFIRMED
        db.expire_all()
        seller_acc, _ = _accounts(db, ctx)
        assert float(seller_acc.reserved_balance) == 400

    def test_clear_cannot_take_reserved_allowance(self, db, two_companies):
        """清缴补扣不能挪用交易占用：冻结不足时只能用自由可用，占用部分留作缺口。"""
        ctx = two_companies
        seller = ctx["seller"]

        # 卖方持仓 1000：确认 300 吨订单占用；无报告直接手动清缴 900 吨
        order = create_order(db, seller.id, ctx["buyer"].id, 2025, 300)
        confirm_order(db, order.id, ctx["buyer"].id)

        # 构造 900 吨排放（无报告场景，clear_emission 按年度核算量清缴）
        db.add(
            ActivityData(
                company_id=seller.id, scope_id=_scope_id(db, seller), year=2025,
                period="monthly", activity_type="外购电力", unit="MWh",
                quantity=round(900 / 0.5703, 6), data_source="台账", verified=1,
            )
        )
        db.commit()
        from app.services.calculation_service import annual_total, recalc_company_year

        recalc_company_year(db, seller.id, 2025)
        emission = annual_total(db, seller.id, 2025)
        record = clear_emission(db, seller.id, 2025, "2025-12-31")

        # 只能从自由可用 700 吨中清缴，占用的 300 吨不动 → 缺口
        assert record.status == "deficit"
        assert float(record.cleared_amount) == approx(700)
        assert float(record.deficit) == approx(emission - 700)
        db.expire_all()
        seller_acc, _ = _accounts(db, ctx)
        assert float(seller_acc.current_balance) == 300
        assert float(seller_acc.reserved_balance) == 300
        _assert_snapshot(db, seller_acc.id)

    def test_cancel_order_after_freeze_still_releases(self, db, two_companies):
        """占用与冻结并存时撤销订单：释放占用，冻结不受影响，不变量保持。"""
        ctx = two_companies
        seller = ctx["seller"]
        order = create_order(db, seller.id, ctx["buyer"].id, 2025, 300)
        confirm_order(db, order.id, ctx["buyer"].id)

        db.add(
            ActivityData(
                company_id=seller.id, scope_id=_scope_id(db, seller), year=2025,
                period="monthly", activity_type="外购电力", unit="MWh",
                quantity=round(700 / 0.5703, 6), data_source="台账", verified=1,
            )
        )
        db.commit()
        from app.services.calculation_service import recalc_company_year

        recalc_company_year(db, seller.id, 2025)
        report = generate_report(db, seller.id, 2025)
        submit_report(db, report)
        approve_report(db, report, verifier_id=1)

        # 持仓 1000 = 冻结 700 + 占用 300；撤销订单后占用归零，冻结仍为 700
        cancel_order(db, order.id, ctx["seller"].id)
        db.expire_all()
        seller_acc, _ = _accounts(db, ctx)
        assert float(seller_acc.current_balance) == 1000
        assert float(seller_acc.frozen_balance) == 700
        assert float(seller_acc.reserved_balance) == 0
        _assert_snapshot(db, seller_acc.id)


def _scope_id(db, company):
    return (
        db.query(EmissionScope)
        .filter(EmissionScope.company_id == company.id, EmissionScope.scope == "2")
        .first()
        .id
    )


class TestOrderConcurrency:
    def test_parallel_deliver_settles_once(self, db, two_companies):
        """买卖双方并发点击交割：只有一次双方入账，其余为幂等返回或竞争失败。"""
        ctx = two_companies
        order = create_order(db, ctx["seller"].id, ctx["buyer"].id, 2025, 300)
        confirm_order(db, order.id, ctx["buyer"].id)
        order_id = order.id
        outcomes = []

        def worker(company_id):
            session = _fresh_session(db)
            try:
                o = deliver_order(session, order_id, company_id)
                outcomes.append(o.status)
            except TradeOrderError:
                # 与首个交割事务竞争状态行失败：合法拒绝，不允许重复入账
                outcomes.append("reject")
            finally:
                session.close()

        with ThreadPoolExecutor(max_workers=6) as pool:
            list(pool.map(worker, [ctx["seller"].id, ctx["buyer"].id] * 3))

        db.expire_all()
        assert outcomes.count(DELIVERED) >= 1
        assert set(outcomes) <= {DELIVERED, "reject"}
        assert db.query(AllowanceTransaction).filter_by(tx_type="trade_deliver_out").count() == 1
        assert db.query(AllowanceTransaction).filter_by(tx_type="trade_deliver_in").count() == 1
        seller_acc, buyer_acc = _accounts(db, ctx)
        assert float(seller_acc.current_balance) == 700
        assert float(buyer_acc.current_balance) == 700
        _assert_snapshot(db, seller_acc.id)
        _assert_snapshot(db, buyer_acc.id)

    def test_deliver_races_cancel_only_one_wins(self, db, two_companies):
        """交割与撤销并发：恰有一方胜出，不允许“已交割又释放占用”的脏账。"""
        ctx = two_companies
        order = create_order(db, ctx["seller"].id, ctx["buyer"].id, 2025, 300)
        confirm_order(db, order.id, ctx["buyer"].id)
        order_id = order.id
        errors = []

        def deliver():
            session = _fresh_session(db)
            try:
                deliver_order(session, order_id, ctx["seller"].id)
                return "delivered"
            except TradeOrderError:
                return "reject"
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
                return "error"
            finally:
                session.close()

        def cancel():
            session = _fresh_session(db)
            try:
                cancel_order(session, order_id, ctx["buyer"].id, reason="并发撤销")
                return "cancelled"
            except TradeOrderError:
                return "reject"
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
                return "error"
            finally:
                session.close()

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda f: f(), [deliver, cancel] * 4))

        db.expire_all()
        assert not errors, f"并发出现非预期异常：{errors!r}"
        final = db.get(TradeOrder, order_id)
        assert final.status in (DELIVERED, CANCELLED)
        # 首个交割/撤销事务抢占状态行胜出，其余调用得到终态或被拒绝，绝不脏写
        seller_acc, buyer_acc = _accounts(db, ctx)
        if final.status == DELIVERED:
            assert "delivered" in results
            assert float(seller_acc.current_balance) == 700
            assert float(seller_acc.reserved_balance) == 0
            assert float(buyer_acc.current_balance) == 700
            assert db.query(AllowanceTransaction).filter_by(tx_type="trade_release").count() == 0
        else:
            assert "cancelled" in results
            assert float(seller_acc.current_balance) == 1000
            assert float(seller_acc.reserved_balance) == 0
            assert float(buyer_acc.current_balance) == 400
            assert db.query(AllowanceTransaction).filter(
                AllowanceTransaction.tx_type.in_(["trade_deliver_out", "trade_deliver_in"])
            ).count() == 0
        _assert_snapshot(db, seller_acc.id)
        _assert_snapshot(db, buyer_acc.id)

    def test_parallel_orders_total_reservation_never_exceeds_available(self, db, two_companies):
        """同一卖方多张订单并发被买方确认：总占用不超过自由可用 1000 吨。"""
        ctx = two_companies
        seller, buyer = ctx["seller"], ctx["buyer"]
        # 挂 6 张各 200 吨的订单（共 1200 > 1000），并发确认
        order_ids = [
            create_order(db, seller.id, buyer.id, 2025, 200).id for _ in range(6)
        ]
        db.commit()

        def worker(oid):
            session = _fresh_session(db)
            try:
                confirm_order(session, oid, buyer.id)
                return "ok"
            except TradeOrderError:
                return "reject"
            finally:
                session.close()

        with ThreadPoolExecutor(max_workers=6) as pool:
            outcomes = list(pool.map(worker, order_ids))

        db.expire_all()
        assert outcomes.count("ok") == 5
        assert outcomes.count("reject") == 1
        seller_acc, _ = _accounts(db, ctx)
        assert float(seller_acc.reserved_balance) == 1000
        assert float(seller_acc.current_balance) == 1000
        confirmed = db.query(TradeOrder).filter_by(status=CONFIRMED).count()
        assert confirmed == 5
        _assert_snapshot(db, seller_acc.id)

        # 被拒订单撤销后无副作用
        rejected_id = order_ids[outcomes.index("reject")]
        session = _fresh_session(db)
        try:
            o = cancel_order(session, rejected_id, seller.id)
            assert o.status == CANCELLED
        finally:
            session.close()
        db.expire_all()
        seller_acc, _ = _accounts(db, ctx)
        assert float(seller_acc.reserved_balance) == 1000

    def test_order_delivery_races_compliance_freeze(self, db, two_companies):
        """订单交割与报告批准冻结并发：结束后三方（持仓/冻结/占用/流水）一致。"""
        ctx = two_companies
        seller = ctx["seller"]

        # 持仓 1000；确认 400 吨订单占用；排放 800 吨
        order = create_order(db, seller.id, ctx["buyer"].id, 2025, 400)
        confirm_order(db, order.id, ctx["buyer"].id)
        db.add(
            ActivityData(
                company_id=seller.id, scope_id=_scope_id(db, seller), year=2025,
                period="monthly", activity_type="外购电力", unit="MWh",
                quantity=round(800 / 0.5703, 6), data_source="台账", verified=1,
            )
        )
        db.commit()
        from app.services.calculation_service import recalc_company_year

        recalc_company_year(db, seller.id, 2025)
        # 先生成并提交报告（使用独立状态，供工作线程批准）
        report = generate_report(db, seller.id, 2025)
        submit_report(db, report)
        db.commit()
        order_id = order.id
        report_id = report.id
        errors = []

        def deliver():
            session = _fresh_session(db)
            try:
                deliver_order(session, order_id, seller.id)
            except Exception as exc:  # noqa: BLE001
                errors.append(("deliver", exc))
            finally:
                session.close()

        def approve():
            session = _fresh_session(db)
            try:
                from app.models import MrvReport

                r = session.get(MrvReport, report_id)
                approve_report(session, r, verifier_id=1)
            except Exception as exc:  # noqa: BLE001
                errors.append(("approve", exc))
            finally:
                session.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(lambda f: f(), [deliver, approve]))

        db.expire_all()
        assert not errors, f"并发出现非预期异常：{errors!r}"
        seller_acc, buyer_acc = _accounts(db, ctx)
        # 先交割：卖方剩 600 自由 → 冻结 600，缺口 200；
        # 先批准：自由可用 600 冻结 600，交割时占用 400 出库（持仓 600=冻结600+占用0）
        assert float(seller_acc.current_balance) + float(buyer_acc.current_balance) == approx(1000 + 400)
        assert float(seller_acc.frozen_balance) + float(seller_acc.reserved_balance) <= float(
            seller_acc.current_balance
        ) + 1e-9
        _assert_snapshot(db, seller_acc.id)
        _assert_snapshot(db, buyer_acc.id)
