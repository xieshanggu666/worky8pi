"""并发交易 / 清缴一致性测试。

覆盖：
- 多线程并发卖出：总额不超过余额，余额与逐笔流水快照严格一致（无超额扣减）；
- 并发卖出中余额不足的部分被拒绝，成功者入账金额之和等于实际扣减；
- 同幂等键重复提交（串行与并发）只入账一次；
- 失败回滚：非法交易不写流水、余额不变；
- 并发清缴只扣一次，重复清缴不重复履约；缺口状态补缴正确；
- 清缴与交易并发时余额与流水依旧一致；
- 账户缺失时清缴不再写入 account_id=0 的脏流水。
"""

from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
from app.models import (
    ActivityData,
    AllowanceAccount,
    AllowanceTransaction,
    ComplianceRecord,
)
from app.services.quota_service import allocate_quota, clear_emission
from app.services.trading_service import transfer


@pytest.fixture()
def db(tmp_path):
    """文件型临时库：多线程各自持有独立连接但共享同一份数据（内存库每连接一份空库）。"""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'concurrency.db'}",
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


def approx(value, rel=1e-6):
    return pytest.approx(float(value), rel=rel)


def _fresh_session(db):
    """基于测试引擎创建新会话，供工作线程使用（同一会话不可跨线程）。"""
    from sqlalchemy.orm import Session

    return Session(bind=db.bind)


def _tx_snapshot_consistent(db, account_id):
    """流水不变量：持仓/冻结快照逐笔可推算，且末笔快照等于账户当前值。"""
    txs = (
        db.query(AllowanceTransaction)
        .filter(AllowanceTransaction.account_id == account_id)
        .order_by(AllowanceTransaction.id.asc())
        .all()
    )
    current_signs = {
        "allocation": 1, "buy": 1, "transfer_in": 1, "reversal": 1,
        "sell": -1, "transfer_out": -1, "offset": -1, "clear": -1,
        "frozen_clear": -1, "freeze": 0,
    }
    frozen_signs = {
        "freeze": 1, "frozen_clear": -1, "reversal": -1,
    }
    expected_current = 0.0
    expected_frozen = 0.0
    for tx in txs:
        expected_current = round(
            expected_current + current_signs.get(tx.tx_type, 0) * float(tx.amount), 4
        )
        expected_frozen = round(
            expected_frozen + frozen_signs.get(tx.tx_type, 0) * float(tx.amount), 4
        )
        assert float(tx.balance_after) == approx(expected_current), (
            f"流水 #{tx.id} 持仓快照 {tx.balance_after} 与推算持仓 {expected_current} 不一致"
        )
        assert float(tx.frozen_after or 0) == approx(expected_frozen), (
            f"流水 #{tx.id} 冻结快照 {tx.frozen_after} 与推算冻结 {expected_frozen} 不一致"
        )
    account = db.get(AllowanceAccount, account_id)
    assert float(account.current_balance) == approx(expected_current)
    assert float(account.frozen_balance) == approx(expected_frozen)
    assert float(account.frozen_balance) <= float(account.current_balance) + 1e-9
    return expected_current, txs


class TestConcurrentTransfer:
    def test_parallel_sell_never_overdraws(self, db, seed):
        """10 个线程各卖 100（初始 800）：恰好 8 笔成功，余额为 0，无超额扣减。"""
        allocate_quota(db, seed["company"].id, 2025, 1000, 800, 0)
        account_id = db.query(AllowanceAccount).first().id

        results = []

        def worker():
            session = _fresh_session(db)
            try:
                account = session.get(AllowanceAccount, account_id)
                tx = transfer(session, account, 100, "sell", counterparty="并发买方", tx_date="2025-06-01")
                results.append(("ok", float(tx.amount)))
            except ValueError:
                results.append(("reject", 0.0))
            finally:
                session.close()

        with ThreadPoolExecutor(max_workers=10) as pool:
            list(pool.map(lambda _: worker(), range(10)))

        db.expire_all()
        assert sum(1 for r in results if r[0] == "ok") == 8
        assert sum(1 for r in results if r[0] == "reject") == 2
        balance, txs = _tx_snapshot_consistent(db, account_id)
        assert balance == approx(0)
        assert sum(float(t.amount) for t in txs if t.tx_type == "sell") == approx(800)

    def test_parallel_sell_partial_balance(self, db, seed):
        """每笔 300、初始 800：2 笔成功 1 笔拒绝，余额 200，流水合计与余额一致。"""
        allocate_quota(db, seed["company"].id, 2025, 1000, 800, 0)
        account_id = db.query(AllowanceAccount).first().id

        def worker():
            session = _fresh_session(db)
            try:
                account = session.get(AllowanceAccount, account_id)
                transfer(session, account, 300, "sell", tx_date="2025-06-01")
                return True
            except ValueError:
                return False
            finally:
                session.close()

        with ThreadPoolExecutor(max_workers=3) as pool:
            outcomes = list(pool.map(lambda _: worker(), range(3)))

        db.expire_all()
        assert outcomes.count(True) == 2
        balance, txs = _tx_snapshot_consistent(db, account_id)
        assert balance == approx(200)

    def test_idempotent_key_serial_duplicate(self, db, seed):
        """相同幂等键的两次串行请求：只入账一次，返回同一笔流水。"""
        allocate_quota(db, seed["company"].id, 2025, 1000, 800, 0)
        account = db.query(AllowanceAccount).first()
        first = transfer(db, account, 100, "sell", tx_date="2025-06-01", idempotency_key="key-001")
        second = transfer(db, account, 100, "sell", tx_date="2025-06-01", idempotency_key="key-001")
        assert first.id == second.id
        db.expire_all()
        assert float(account.current_balance) == approx(700)
        assert db.query(AllowanceTransaction).filter(AllowanceTransaction.tx_type == "sell").count() == 1

    def test_idempotent_key_parallel_duplicate(self, db, seed):
        """相同幂等键并发提交：只有一笔真正入账。"""
        allocate_quota(db, seed["company"].id, 2025, 1000, 800, 0)
        account_id = db.query(AllowanceAccount).first().id
        tx_ids = []

        def worker():
            session = _fresh_session(db)
            try:
                account = session.get(AllowanceAccount, account_id)
                tx = transfer(session, account, 100, "sell", tx_date="2025-06-01", idempotency_key="same-key")
                tx_ids.append(tx.id)
            finally:
                session.close()

        with ThreadPoolExecutor(max_workers=5) as pool:
            list(pool.map(lambda _: worker(), range(5)))

        db.expire_all()
        assert set(tx_ids) == {tx_ids[0]}
        assert len(tx_ids) == 5
        balance, _ = _tx_snapshot_consistent(db, account_id)
        assert balance == approx(700)

    def test_failed_transfer_rolls_back(self, db, seed):
        """余额不足时事务回滚：不产生流水，余额保持不变。"""
        allocate_quota(db, seed["company"].id, 2025, 1000, 800, 0)
        account = db.query(AllowanceAccount).first()
        with pytest.raises(ValueError, match="余额不足"):
            transfer(db, account, 900, "sell", tx_date="2025-06-01")
        assert float(account.current_balance) == approx(800)
        assert db.query(AllowanceTransaction).filter(AllowanceTransaction.tx_type == "sell").count() == 0
        _tx_snapshot_consistent(db, account.id)

    def test_invalid_amount_no_side_effect(self, db, seed):
        """非法金额（0/负数）直接拒绝，无任何副作用。"""
        allocate_quota(db, seed["company"].id, 2025, 1000, 800, 0)
        account = db.query(AllowanceAccount).first()
        with pytest.raises(ValueError, match="正数"):
            transfer(db, account, 0, "sell")
        with pytest.raises(ValueError, match="正数"):
            transfer(db, account, -5, "sell")
        assert float(account.current_balance) == approx(800)


class TestConcurrentClear:
    def _prepare(self, db, seed, activity_qty=2000, quota=800):
        db.add(
            ActivityData(
                company_id=seed["company"].id, scope_id=seed["scope2"].id, year=2025,
                period="monthly", activity_type="外购电力", unit="MWh",
                quantity=activity_qty, data_source="台账", verified=1,
            )
        )
        db.commit()
        from app.services.calculation_service import annual_total, recalc_company_year

        recalc_company_year(db, seed["company"].id, 2025)
        allocate_quota(db, seed["company"].id, 2025, 1000, quota, 0)
        return annual_total(db, seed["company"].id, 2025)

    def test_parallel_clear_deducts_once(self, db, seed):
        """配额充足时并发清缴：只扣一次、一条 clear 流水、一条履约记录。"""
        emission = self._prepare(db, seed, activity_qty=1000, quota=1000)
        account_id = db.query(AllowanceAccount).first().id

        def worker():
            session = _fresh_session(db)
            try:
                rec = clear_emission(session, seed["company"].id, 2025, "2025-12-31")
                # 在会话内取走标量，避免会话关闭后访问游离对象
                return rec.id, rec.status, float(rec.cleared_amount)
            finally:
                session.close()

        with ThreadPoolExecutor(max_workers=5) as pool:
            records = list(pool.map(lambda _: worker(), range(5)))

        db.expire_all()
        assert len({rid for rid, _, _ in records}) == 1
        for _, status, cleared in records:
            assert status == "compliant"
            assert cleared == approx(emission)
        balance, txs = _tx_snapshot_consistent(db, account_id)
        clears = [t for t in txs if t.tx_type == "clear"]
        assert len(clears) == 1
        assert balance == approx(1000 - emission)
        assert db.query(ComplianceRecord).count() == 1

    def test_duplicate_clear_after_compliant_is_noop(self, db, seed):
        """达标后再次清缴（不同请求、无幂等键）：幂等无操作，不重复履约。"""
        emission = self._prepare(db, seed, activity_qty=1000, quota=1000)
        account_id = db.query(AllowanceAccount).first().id
        first = clear_emission(db, seed["company"].id, 2025, "2025-12-31")
        again = clear_emission(db, seed["company"].id, 2025, "2025-12-31")
        assert first.id == again.id
        db.expire_all()
        assert float(again.cleared_amount) == approx(emission)
        assert db.query(AllowanceTransaction).filter(AllowanceTransaction.tx_type == "clear").count() == 1
        balance, _ = _tx_snapshot_consistent(db, account_id)
        assert balance == approx(1000 - emission)

    def test_deficit_then_topup_clear(self, db, seed):
        """缺口清缴后，补缴配额再清缴：仅扣剩余缺口，累计不超过排放量。"""
        emission = self._prepare(db, seed, activity_qty=2000, quota=800)
        account = db.query(AllowanceAccount).first()
        first = clear_emission(db, seed["company"].id, 2025, "2025-12-31")
        assert first.status == "deficit"
        assert float(first.cleared_amount) == approx(800)

        # 模拟从市场买入 600 吨后补缴
        transfer(db, account, 600, "buy", counterparty="交易所", tx_date="2025-12-20")
        second = clear_emission(db, seed["company"].id, 2025, "2025-12-31")
        assert second.status == "compliant"
        assert float(second.cleared_amount) == approx(emission)
        db.expire_all()
        # 两次清缴合计恰为排放量，不多扣
        clears = db.query(AllowanceTransaction).filter(AllowanceTransaction.tx_type == "clear").all()
        assert sum(float(t.amount) for t in clears) == approx(emission)
        # 买入 600，补缴 emission-800，剩余为 600-(emission-800)
        balance, _ = _tx_snapshot_consistent(db, account.id)
        assert balance == approx(600 - (emission - 800))

    def test_clear_with_same_idempotency_key_parallel(self, db, seed):
        """相同幂等键并发清缴：返回同一记录，仅一次扣减。"""
        self._prepare(db, seed, activity_qty=1000, quota=1000)
        account_id = db.query(AllowanceAccount).first().id

        def worker():
            session = _fresh_session(db)
            try:
                return clear_emission(
                    session, seed["company"].id, 2025, "2025-12-31", idempotency_key="clear-key-1"
                ).id
            finally:
                session.close()

        with ThreadPoolExecutor(max_workers=5) as pool:
            ids = list(pool.map(lambda _: worker(), range(5)))

        db.expire_all()
        assert set(ids) == {ids[0]}
        assert db.query(AllowanceTransaction).filter(AllowanceTransaction.tx_type == "clear").count() == 1
        _tx_snapshot_consistent(db, account_id)

    def test_clear_and_transfer_concurrent_consistency(self, db, seed):
        """清缴与卖出并发：结束后余额、流水快照、履约记录三方一致。"""
        self._prepare(db, seed, activity_qty=1000, quota=1000)
        account_id = db.query(AllowanceAccount).first().id
        errors = []

        def do_clear():
            session = _fresh_session(db)
            try:
                clear_emission(session, seed["company"].id, 2025, "2025-12-31")
            except Exception as exc:  # noqa: BLE001 - 并发冲突允许失败，但不得脏写
                errors.append(exc)
            finally:
                session.close()

        def do_sell():
            session = _fresh_session(db)
            try:
                account = session.get(AllowanceAccount, account_id)
                transfer(session, account, 200, "sell", tx_date="2025-12-30")
            except ValueError:
                pass  # 余额不足是合法结果
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
            finally:
                session.close()

        with ThreadPoolExecutor(max_workers=6) as pool:
            futures = [pool.submit(do_clear)] + [pool.submit(do_sell) for _ in range(5)]
            for f in futures:
                f.result()

        db.expire_all()
        assert not errors, f"并发执行出现非预期异常：{errors!r}"
        _tx_snapshot_consistent(db, account_id)
        record = db.query(ComplianceRecord).one()
        # 履约扣减与卖出扣减之和不超过初始余额 1000
        cleared = float(record.cleared_amount)
        sold = sum(
            float(t.amount)
            for t in db.query(AllowanceTransaction).filter(AllowanceTransaction.tx_type == "sell").all()
        )
        assert cleared + sold <= 1000 + 1e-6
        assert float(db.get(AllowanceAccount, account_id).current_balance) == approx(1000 - cleared - sold)

    def test_clear_without_account_creates_no_dirty_tx(self, db, seed):
        """无配额账户时清缴：记全额缺口，不产生 account_id=0 的脏流水。"""
        db.add(
            ActivityData(
                company_id=seed["company"].id, scope_id=seed["scope2"].id, year=2025,
                period="monthly", activity_type="外购电力", unit="MWh",
                quantity=1000, data_source="台账", verified=1,
            )
        )
        db.commit()
        from app.services.calculation_service import annual_total, recalc_company_year

        recalc_company_year(db, seed["company"].id, 2025)
        emission = annual_total(db, seed["company"].id, 2025)
        record = clear_emission(db, seed["company"].id, 2025, "2025-12-31")
        assert record.status == "deficit"
        assert float(record.deficit) == approx(emission)
        assert db.query(AllowanceTransaction).count() == 0
