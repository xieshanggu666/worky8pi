"""年度配额闭环测试：企业间订单交割联动履约清缴。

覆盖闭环主线
============
买方报告批准形成“冻结 + 缺口” → 买入订单交割 → 同事务内核销冻结配额、
用刚到账配额补缴缺口 → 履约状态达标、配额状态 cleared、仪表盘统计同步。

覆盖：
- 交割自动核销：足额补缴达标 / 部分补缴仍缺口 / 超买余量留存 / 纯冻结记录核销；
- 买方无履约记录时交割行为不变；auto_clear_deficit=False 时不联动，可事后手动清缴；
- 卖方自身履约记录不被买方订单交割触动；
- 重复交割幂等：缺口只核销一次；
- 多线程：两笔订单交割与手动清缴并发，结束后余额、流水三类快照、
  履约记录、仪表盘统计四方一致，且“持仓 + 已清缴 = 年度分配总量”守恒；
- 交割联动补扣流水（trade_deficit_clear）与冻结核销流水均关联订单，快照链可推算。
"""

from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import create_engine, event, func
from sqlalchemy.orm import Session, sessionmaker

from app.core.database import Base
from app.models import (
    ActivityData,
    AllowanceAccount,
    AllowanceTransaction,
    CalculationMethod,
    Company,
    ComplianceRecord,
    EmissionFactor,
    EmissionScope,
    MrvReport,
    Quota,
)
from app.services.calculation_service import annual_total, recalc_company_year
from app.services.mrv_service import (
    approve_report,
    generate_report,
    reverse_report,
    submit_report,
)
from app.services.quota_service import allocate_quota, clear_emission
from app.services.stats_service import dashboard_stats
from app.services.trade_order_service import (
    CONFIRMED,
    DELIVERED,
    TradeOrderError,
    confirm_order,
    create_order,
    deliver_order,
)

FACTOR = 0.5703


def approx(value, rel=1e-6):
    return pytest.approx(float(value), rel=rel)


@pytest.fixture()
def db(tmp_path):
    """文件型临时库：多线程共享同一份数据。"""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'delivery_loop.db'}",
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
def two_companies(db):
    """卖方 1000 吨、买方 400 吨（2025 年度），双方均有外购电力核算边界。"""
    seller = Company(code="SELL-001", name="卖方企业", industry="电力", region="华东")
    buyer = Company(code="BUY-001", name="买方企业", industry="水泥", region="华北")
    db.add_all([seller, buyer])
    db.flush()

    for c in (seller, buyer):
        db.add(
            EmissionScope(company_id=c.id, scope="2", category="外购电力", name="厂区用电")
        )
    db.add(
        CalculationMethod(
            method_code="ELEC", name="外购电力排放因子法", scope="2", formula_type="activity_factor"
        )
    )
    db.add(
        EmissionFactor(
            factor_code="ELEC-GRID", name="外购电力", scope="2", unit="tCO2/MWh", value=FACTOR,
            source="电网因子", valid_from="2024-01-01", valid_to="2025-12-31",
        )
    )
    allocate_quota(db, seller.id, 2025, baseline=1000, allocation_amount=1000)
    allocate_quota(db, buyer.id, 2025, baseline=400, allocation_amount=400)
    db.commit()
    db.expire_all()
    return {"seller": seller, "buyer": buyer}


def _account(db, company_id):
    return (
        db.query(AllowanceAccount)
        .filter_by(company_id=company_id, year=2025)
        .one()
    )


def _scope_id(db, company):
    return (
        db.query(EmissionScope)
        .filter(EmissionScope.company_id == company.id, EmissionScope.scope == "2")
        .first()
        .id
    )


def _approve_emission(db, company, emission):
    """录入活动量并把年度报告走到批准（按目标排放量反算活动量）。"""
    db.add(
        ActivityData(
            company_id=company.id,
            scope_id=_scope_id(db, company),
            year=2025,
            period="monthly",
            activity_type="外购电力",
            unit="MWh",
            quantity=round(emission / FACTOR, 6),
            data_source="台账",
            verified=1,
        )
    )
    db.commit()
    recalc_company_year(db, company.id, 2025)
    assert annual_total(db, company.id, 2025) == approx(emission)
    report = generate_report(db, company.id, 2025)
    submit_report(db, report)
    approve_report(db, report, verifier_id=1)
    db.expire_all()
    return (
        db.query(ComplianceRecord)
        .filter_by(company_id=company.id, year=2025, is_active=1)
        .one()
    )


def _make_delivered(db, ctx, amount, **kwargs):
    order = create_order(
        db, ctx["seller"].id, ctx["buyer"].id, 2025, amount, **kwargs
    )
    confirm_order(db, order.id, ctx["buyer"].id)
    return deliver_order(db, order.id, ctx["seller"].id)


def _active_record(db, company_id):
    return (
        db.query(ComplianceRecord)
        .filter_by(company_id=company_id, year=2025, is_active=1)
        .one()
    )


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
        "frozen_clear": -1, "trade_deliver_out": -1, "trade_deficit_clear": -1,
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
    assert float(account.frozen_balance) + float(account.reserved_balance) <= float(
        account.current_balance
    ) + 1e-9
    return exp_c, exp_f, exp_r, txs


class TestDeliveryClearsBuyerDeficit:
    def test_full_deficit_closed_by_delivery(self, db, two_companies):
        """买方排放 800（冻结 400、缺口 400）：买入 400 交割后同事务足额清缴达标。"""
        ctx = two_companies
        buyer, seller = ctx["buyer"], ctx["seller"]

        record = _approve_emission(db, buyer, 800)
        assert float(record.frozen_amount) == approx(400)
        assert float(record.deficit) == approx(400)
        assert record.status == "deficit"

        order = _make_delivered(db, ctx, 400, price=80)
        assert order.status == DELIVERED
        assert bool(order.auto_clear_deficit) is True

        db.expire_all()
        seller_acc, buyer_acc = _account(db, seller.id), _account(db, buyer.id)
        # 卖方 1000-400；买方 400+400-400冻结核销-400缺口补缴 = 0
        assert float(seller_acc.current_balance) == approx(600)
        assert float(buyer_acc.current_balance) == approx(0)
        assert float(buyer_acc.frozen_balance) == approx(0)
        assert float(buyer_acc.reserved_balance) == approx(0)

        record = _active_record(db, buyer.id)
        assert record.status == "compliant"
        assert float(record.cleared_amount) == approx(800)
        assert float(record.frozen_amount) == approx(0)
        assert float(record.deficit) == approx(0)
        # 配额状态同步闭环
        quota = db.query(Quota).filter_by(company_id=buyer.id, year=2025).one()
        assert quota.status == "cleared"

        # 联动补扣流水关联订单、价格与交割日期，且先有冻结核销流水
        buyer_txs = (
            db.query(AllowanceTransaction)
            .filter(AllowanceTransaction.account_id == buyer_acc.id)
            .order_by(AllowanceTransaction.id.asc())
            .all()
        )
        types = [t.tx_type for t in buyer_txs]
        assert types.count("frozen_clear") == 1
        deficit_tx = next(t for t in buyer_txs if t.tx_type == "trade_deficit_clear")
        assert float(deficit_tx.amount) == approx(400)
        assert deficit_tx.trade_order_id == order.id
        assert float(deficit_tx.price) == approx(80)
        assert deficit_tx.tx_date == order.tx_date
        assert deficit_tx.balance_after == 0
        _assert_snapshot(db, seller_acc.id)
        _assert_snapshot(db, buyer_acc.id)

    def test_partial_delivery_leaves_remaining_deficit(self, db, two_companies):
        """到账不足以覆盖缺口：先核销全部冻结，再尽力补缴，剩余缺口保留 deficit。"""
        ctx = two_companies
        buyer = ctx["buyer"]
        _approve_emission(db, buyer, 800)  # 冻结400 缺口400

        order = _make_delivered(db, ctx, 200)
        assert order.status == DELIVERED

        db.expire_all()
        buyer_acc = _account(db, buyer.id)
        # 400 + 200 到账；冻结核销 400，可用补扣 200 → 持仓 0、缺口剩 200
        assert float(buyer_acc.current_balance) == approx(0)
        record = _active_record(db, buyer.id)
        assert record.status == "deficit"
        assert float(record.cleared_amount) == approx(600)
        assert float(record.deficit) == approx(200)
        _assert_snapshot(db, buyer_acc.id)

        # 再买入 200 吨的第二张订单：剩余缺口闭环达标
        _make_delivered(db, ctx, 200)
        db.expire_all()
        record = _active_record(db, buyer.id)
        assert record.status == "compliant"
        assert float(record.cleared_amount) == approx(800)
        assert float(record.deficit) == approx(0)
        assert float(_account(db, buyer.id).current_balance) == approx(0)
        _assert_snapshot(db, _account(db, buyer.id).id)

    def test_excess_purchase_keeps_surplus_available(self, db, two_companies):
        """买入量超过缺口：达标后多余配额留在自由可用，不多扣。"""
        ctx = two_companies
        buyer = ctx["buyer"]
        _approve_emission(db, buyer, 800)  # 冻结400 缺口400

        _make_delivered(db, ctx, 600)
        db.expire_all()
        buyer_acc = _account(db, buyer.id)
        # 400 + 600 - 400冻结核销 - 400缺口补缴 = 200 余量
        assert float(buyer_acc.current_balance) == approx(200)
        record = _active_record(db, buyer.id)
        assert record.status == "compliant"
        assert float(record.cleared_amount) == approx(800)
        assert float(record.deficit) == approx(0)
        _assert_snapshot(db, buyer_acc.id)

    def test_pending_fully_frozen_record_cleared_on_delivery(self, db, two_companies):
        """买方义务已被冻结全额覆盖（pending、无缺口）：交割触发冻结核销并达标。"""
        ctx = two_companies
        buyer = ctx["buyer"]
        record = _approve_emission(db, buyer, 400)  # 配额恰好 400，全额冻结
        assert float(record.frozen_amount) == approx(400)
        assert float(record.deficit) == approx(0)
        assert record.status == "pending"

        _make_delivered(db, ctx, 100)
        db.expire_all()
        buyer_acc = _account(db, buyer.id)
        # 400 冻结全部核销（-400/-400），买入 100 留存
        assert float(buyer_acc.current_balance) == approx(100)
        assert float(buyer_acc.frozen_balance) == approx(0)
        record = _active_record(db, buyer.id)
        assert record.status == "compliant"
        assert float(record.cleared_amount) == approx(400)
        _assert_snapshot(db, buyer_acc.id)

    def test_no_compliance_record_delivery_unchanged(self, db, two_companies):
        """买方没有履约记录（未批准报告/清缴）：交割只到账，不凭空生成履约记录。"""
        ctx = two_companies
        buyer = ctx["buyer"]
        _make_delivered(db, ctx, 300)
        db.expire_all()
        assert float(_account(db, buyer.id).current_balance) == approx(700)
        assert db.query(ComplianceRecord).filter_by(company_id=buyer.id).count() == 0
        buyer_txs = db.query(AllowanceTransaction).filter(
            AllowanceTransaction.company_id == buyer.id
        ).all()
        assert {t.tx_type for t in buyer_txs} <= {"allocation", "trade_deliver_in"}
        _assert_snapshot(db, _account(db, buyer.id).id)

    def test_auto_clear_disabled_then_manual_clear(self, db, two_companies):
        """挂单关闭联动：交割只到账、缺口保留；事后手动清缴同样闭环。"""
        ctx = two_companies
        buyer = ctx["buyer"]
        _approve_emission(db, buyer, 800)  # 冻结400 缺口400

        order = _make_delivered(db, ctx, 400, auto_clear_deficit=False)
        assert bool(order.auto_clear_deficit) is False
        db.expire_all()
        buyer_acc = _account(db, buyer.id)
        assert float(buyer_acc.current_balance) == approx(800)
        assert float(buyer_acc.frozen_balance) == approx(400)
        record = _active_record(db, buyer.id)
        assert record.status == "deficit"
        assert float(record.cleared_amount) == approx(0)
        assert not db.query(AllowanceTransaction).filter(
            AllowanceTransaction.tx_type.in_(["trade_deficit_clear", "frozen_clear"])
        ).first()

        # 手动清缴：冻结 400 + 可用补扣 400，达标
        record = clear_emission(db, buyer.id, 2025, "2025-12-31")
        assert record.status == "compliant"
        db.expire_all()
        assert float(_account(db, buyer.id).current_balance) == approx(0)
        _assert_snapshot(db, buyer_acc.id)

    def test_seller_compliance_untouched_by_buyer_delivery(self, db, two_companies):
        """卖方自身的履约缺口不因其卖出交割而被动核销（清缴义务属于各企业自身）。"""
        ctx = two_companies
        seller, buyer = ctx["seller"], ctx["buyer"]
        seller_record = _approve_emission(db, seller, 800)  # 冻结800，自由可用200
        buyer_record = _approve_emission(db, buyer, 600)    # 冻结400 缺口200

        _make_delivered(db, ctx, 200)
        db.expire_all()

        seller_record = _active_record(db, seller.id)
        assert seller_record.status == "pending"
        assert float(seller_record.cleared_amount) == approx(0)
        assert float(seller_record.frozen_amount) == approx(800)
        # 卖方只有占用与出库流水，没有任何清缴类流水
        seller_txs = {
            t.tx_type
            for t in db.query(AllowanceTransaction).filter(
                AllowanceTransaction.company_id == seller.id
            ).all()
        }
        assert not seller_txs.intersection({"clear", "frozen_clear", "trade_deficit_clear"})

        buyer_record = _active_record(db, buyer.id)
        assert buyer_record.status == "compliant"
        _assert_snapshot(db, _account(db, seller.id).id)
        _assert_snapshot(db, _account(db, buyer.id).id)

    def test_repeated_delivery_settles_deficit_once(self, db, two_companies):
        """重复交割幂等：缺口只随首次交割核销一次，不产生重复清缴流水。"""
        ctx = two_companies
        buyer = ctx["buyer"]
        _approve_emission(db, buyer, 700)  # 冻结400 缺口300

        order = create_order(db, ctx["seller"].id, buyer.id, 2025, 300)
        confirm_order(db, order.id, buyer.id)
        deliver_order(db, order.id, ctx["seller"].id)
        again = deliver_order(db, order.id, buyer.id)  # 已交割，幂等返回
        assert again.id == order.id

        db.expire_all()
        assert _active_record(db, buyer.id).status == "compliant"
        buyer_acc_id = _account(db, buyer.id).id
        for tx_type in ("frozen_clear", "trade_deficit_clear"):
            assert db.query(AllowanceTransaction).filter_by(
                account_id=buyer_acc_id, tx_type=tx_type
            ).count() == 1
        _assert_snapshot(db, buyer_acc_id)

    def test_delivery_after_manual_topup_then_no_double_clear(self, db, two_companies):
        """先手动部分清缴形成缺口，再交割：交割只核销剩余义务，累计不超过排放量。"""
        ctx = two_companies
        buyer = ctx["buyer"]
        # 排放 800、配额 400：批准冻结 400。先手动清缴（冻结 400 核销，缺口剩 400）
        _approve_emission(db, buyer, 800)
        first = clear_emission(db, buyer.id, 2025, "2025-09-30")
        assert first.status == "deficit"
        assert float(first.cleared_amount) == approx(400)

        # 买入 500 吨交割：只需补缴剩余 400，100 吨余量留存
        _make_delivered(db, ctx, 500)
        db.expire_all()
        record = _active_record(db, buyer.id)
        assert record.status == "compliant"
        assert float(record.cleared_amount) == approx(800)
        assert float(_account(db, buyer.id).current_balance) == approx(100)
        _assert_snapshot(db, _account(db, buyer.id).id)

    def test_reversal_after_delivery_clearance_refunds_bought_allowance(self, db, two_companies):
        """闭环后报告冲正：买入补缴的配额与冻结核销配额一并退还，履约归档。"""
        ctx = two_companies
        buyer = ctx["buyer"]
        _approve_emission(db, buyer, 800)  # 冻结400 缺口400
        _make_delivered(db, ctx, 400)      # 买入400 补缴，达标
        db.expire_all()
        assert _active_record(db, buyer.id).status == "compliant"

        report = (
            db.query(MrvReport)
            .filter_by(company_id=buyer.id, year=2025)
            .one()
        )
        reverse_report(db, report, operator_id=2, reason="排放数据更正")
        db.expire_all()

        buyer_acc = _account(db, buyer.id)
        # 退还：解冻 0 + 退还已清缴 800（400 原冻结 + 400 买入到账）
        assert float(buyer_acc.current_balance) == approx(800)
        assert float(buyer_acc.frozen_balance) == approx(0)
        record = (
            db.query(ComplianceRecord)
            .filter_by(company_id=buyer.id, year=2025)
            .order_by(ComplianceRecord.id.desc())
            .first()
        )
        assert record.status == "reversed"
        assert int(record.is_active) == 0
        _assert_snapshot(db, buyer_acc.id)


class TestDeliveryClearanceConcurrency:
    def test_parallel_deliveries_and_clear_end_consistent(self, db, two_companies):
        """两笔订单交割与手动清缴并发：最终达标，余额/流水/履约/统计四方一致。"""
        ctx = two_companies
        seller, buyer = ctx["seller"], ctx["buyer"]
        _approve_emission(db, buyer, 800)  # 冻结400 缺口400

        # 两张各 200 吨订单，卖方共占用 400（自由可用 1000 足够）
        o1 = create_order(db, seller.id, buyer.id, 2025, 200)
        o2 = create_order(db, seller.id, buyer.id, 2025, 200)
        confirm_order(db, o1.id, buyer.id)
        confirm_order(db, o2.id, buyer.id)
        db.commit()
        ids = [o1.id, o2.id]
        errors = []

        def deliver(oid):
            session = _fresh_session(db)
            try:
                deliver_order(session, oid, seller.id)
            except TradeOrderError as exc:
                errors.append(("deliver", exc))
            except Exception as exc:  # noqa: BLE001
                errors.append(("deliver-x", exc))
            finally:
                session.close()

        def manual_clear():
            session = _fresh_session(db)
            try:
                clear_emission(session, buyer.id, 2025, "2025-12-31")
            except Exception as exc:  # noqa: BLE001 - 锁竞争允许失败，但不得脏写
                errors.append(("clear", exc))
            finally:
                session.close()

        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = [
                pool.submit(deliver, ids[0]),
                pool.submit(deliver, ids[1]),
                pool.submit(manual_clear),
            ]
            for f in futures:
                f.result()

        db.expire_all()
        assert not errors, f"并发执行出现非预期异常：{errors!r}"

        seller_acc, buyer_acc = _account(db, seller.id), _account(db, buyer.id)
        # 卖方出 400；买方 400 初始 + 400 买入 - 800 清缴 = 0
        assert float(seller_acc.current_balance) == approx(600)
        assert float(seller_acc.reserved_balance) == approx(0)
        assert float(buyer_acc.current_balance) == approx(0)
        assert float(buyer_acc.frozen_balance) == approx(0)
        _assert_snapshot(db, seller_acc.id)
        _assert_snapshot(db, buyer_acc.id)

        record = _active_record(db, buyer.id)
        assert record.status == "compliant"
        assert float(record.cleared_amount) == approx(800)
        assert float(record.deficit) == approx(0)

        # 清缴流水总额恰为排放量，不允许重复清缴
        cleared_sum = sum(
            float(t.amount)
            for t in db.query(AllowanceTransaction).filter(
                AllowanceTransaction.company_id == buyer.id,
                AllowanceTransaction.tx_type.in_(["clear", "frozen_clear", "trade_deficit_clear"]),
            ).all()
        )
        assert cleared_sum == approx(800)

    def test_dashboard_stats_consistent_after_closed_loop(self, db, two_companies):
        """闭环后仪表盘统计：已清缴、持仓、冻结、占用、达标数与台账完全一致。"""
        ctx = two_companies
        seller, buyer = ctx["seller"], ctx["buyer"]
        _approve_emission(db, buyer, 800)  # 冻结400 缺口400
        _make_delivered(db, ctx, 400)
        db.expire_all()

        stats = dashboard_stats(db, year=2025)
        assert stats["compliance_counts"]["compliant"] == 1
        assert stats["compliance_counts"]["deficit"] == 0
        assert stats["compliance_counts"]["pending"] == 0
        # 买方清缴 800；卖方未履约
        assert stats["cleared_total"] == approx(800)
        assert stats["frozen_total"] == approx(0)
        assert stats["reserved_total"] == approx(0)
        # 持仓：卖方 600 + 买方 0
        assert stats["current_balance_total"] == approx(600)
        assert stats["available_total"] == approx(600)

        # 年度配额守恒：分配总量 = 当前持仓 + 已清缴（清缴配额已出库至主管部门）
        quota_total = float(
            db.query(func.sum(Quota.total))
            .filter(Quota.year == 2025)
            .scalar()
        )
        assert stats["current_balance_total"] + stats["cleared_total"] == approx(quota_total)

        # 企业边界：买方视角只统计本企业
        buyer_user = type("U", (), {"role": "enterprise", "company_id": buyer.id})()
        own_stats = dashboard_stats(db, year=2025, user=buyer_user)
        assert own_stats["current_balance_total"] == approx(0)
        assert own_stats["cleared_total"] == approx(800)
        assert own_stats["compliance_counts"]["compliant"] == 1


def _fresh_session(db):
    return Session(bind=db.bind)
