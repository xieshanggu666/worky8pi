"""碳排放系统业务逻辑测试：核算引擎 / 配额履约 / 交易台账 / MRV 报告。"""

import pytest

from app.models import ActivityData, EmissionResult
from app.services.calculation_service import (
    annual_total,
    get_factor_for_year,
    recalc_company_year,
    scope_totals,
)
from app.services.mrv_service import (
    approve_report,
    generate_report,
    reverse_report,
    submit_report,
)
from app.services.quota_service import allocate_quota, clear_emission
from app.services.trading_service import transfer


def approx(value, rel=1e-6):
    """Numeric 列返回 Decimal，统一转为 float 后近似比较。"""
    return pytest.approx(float(value), rel=rel)


def _add_activity(db, seed, scope, year, atype, qty, unit="t", verified=1):
    act = ActivityData(
        company_id=seed["company"].id,
        scope_id=scope.id,
        year=year,
        period="monthly",
        activity_type=atype,
        unit=unit,
        quantity=qty,
        data_source="测试台账",
        verified=verified,
    )
    db.add(act)
    db.commit()
    return act


class TestCalculationEngine:
    def test_activity_factor_method(self, db, seed):
        """外购电力：排放量 = 活动量 × 因子值。"""
        _add_activity(db, seed, seed["scope2"], 2025, "外购电力", 2000, "MWh")
        count = recalc_company_year(db, seed["company"].id, 2025)
        assert count == 1
        result = db.query(EmissionResult).first()
        assert float(result.emission_amount) == approx(2000 * 0.5703, rel=1e-6)

    def test_fuel_combustion_method(self, db, seed):
        """燃煤：排放量 = 燃料量 × 综合系数 × 碳氧化率 × 44/12。"""
        _add_activity(db, seed, seed["scope1"], 2025, "燃煤消耗", 100)
        recalc_company_year(db, seed["company"].id, 2025)
        result = db.query(EmissionResult).first()
        expected = 100 * 2.6 * 0.98 * 44 / 12
        assert float(result.emission_amount) == approx(expected, rel=1e-6)

    def test_scope_totals_grouping(self, db, seed):
        """范围一与范围二分别汇总，年度合计正确。"""
        _add_activity(db, seed, seed["scope2"], 2025, "外购电力", 2000, "MWh")
        _add_activity(db, seed, seed["scope1"], 2025, "燃煤消耗", 100)
        recalc_company_year(db, seed["company"].id, 2025)
        totals = scope_totals(db, seed["company"].id, 2025)
        assert totals["2"] == approx(2000 * 0.5703, rel=1e-6)
        assert totals["1"] == approx(100 * 2.6 * 0.98 * 44 / 12, rel=1e-6)
        assert annual_total(db, seed["company"].id, 2025) == approx(totals["1"] + totals["2"], rel=1e-6)

    def test_factor_picked_by_year(self, db, seed):
        """因子按年度生效区间取当期版本。"""
        from app.models import EmissionFactor

        new_factor = EmissionFactor(
            factor_code="COAL-PWR-2025", name="燃煤消耗", scope="1", unit="tC/t", value=2.8,
            source="2025 修订", valid_from="2025-01-01", valid_to=None,
        )
        db.add(new_factor)
        db.commit()

        f_2024 = get_factor_for_year(db, "燃煤消耗", 2024)
        f_2025 = get_factor_for_year(db, "燃煤消耗", 2025)
        assert float(f_2024.value) == approx(2.6)
        assert float(f_2025.value) == approx(2.8)

    def test_recalc_is_idempotent(self, db, seed):
        """重复核算不产生重复结果。"""
        _add_activity(db, seed, seed["scope2"], 2025, "外购电力", 2000, "MWh")
        recalc_company_year(db, seed["company"].id, 2025)
        first = db.query(EmissionResult).count()
        recalc_company_year(db, seed["company"].id, 2025)
        assert db.query(EmissionResult).count() == first == 1

    def test_unverified_activity_excluded_from_calculation(self, db, seed):
        """未核验活动数据不得进入核算：结果表与年度合计均不含该数据。"""
        _add_activity(db, seed, seed["scope2"], 2025, "外购电力", 2000, "MWh", verified=0)
        count = recalc_company_year(db, seed["company"].id, 2025)
        assert count == 0
        assert db.query(EmissionResult).count() == 0
        assert scope_totals(db, seed["company"].id, 2025)["2"] == 0
        assert annual_total(db, seed["company"].id, 2025) == 0

    def test_only_verified_activities_are_calculated(self, db, seed):
        """已核验与未核验数据并存时，仅已核验部分进入核算。"""
        _add_activity(db, seed, seed["scope2"], 2025, "外购电力", 2000, "MWh", verified=1)
        _add_activity(db, seed, seed["scope2"], 2025, "外购电力", 5000, "MWh", verified=0)
        count = recalc_company_year(db, seed["company"].id, 2025)
        assert count == 1
        assert annual_total(db, seed["company"].id, 2025) == approx(2000 * 0.5703, rel=1e-6)

    def test_verify_then_recalc_includes_activity(self, db, seed):
        """数据核验后重新核算方可计入结果。"""
        act = _add_activity(db, seed, seed["scope2"], 2025, "外购电力", 2000, "MWh", verified=0)
        recalc_company_year(db, seed["company"].id, 2025)
        assert annual_total(db, seed["company"].id, 2025) == 0

        act.verified = 1
        db.commit()
        recalc_company_year(db, seed["company"].id, 2025)
        assert annual_total(db, seed["company"].id, 2025) == approx(2000 * 0.5703, rel=1e-6)

    def test_new_unverified_activity_removed_on_recalc(self, db, seed):
        """已核算数据新增未核验记录后重算：旧结果保留、未核验记录不污染。"""
        _add_activity(db, seed, seed["scope2"], 2025, "外购电力", 2000, "MWh", verified=1)
        recalc_company_year(db, seed["company"].id, 2025)
        _add_activity(db, seed, seed["scope1"], 2025, "燃煤消耗", 100, verified=0)
        recalc_company_year(db, seed["company"].id, 2025)
        assert db.query(EmissionResult).count() == 1
        assert annual_total(db, seed["company"].id, 2025) == approx(2000 * 0.5703, rel=1e-6)


class TestQuotaAndCompliance:
    def test_allocation_total_and_account(self, db, seed):
        """配额总额 = 分配量 + 调整量，账户余额入账并登记流水。"""
        quota = allocate_quota(db, seed["company"].id, 2025, baseline=1000, allocation_amount=800, adjustment=-50)
        assert quota.total == approx(750)
        assert quota.status == "allocated"
        from app.models import AllowanceAccount, AllowanceTransaction

        account = db.query(AllowanceAccount).filter(AllowanceAccount.company_id == seed["company"].id).first()
        assert account.opening_balance == approx(750)
        assert account.current_balance == approx(750)
        tx = db.query(AllowanceTransaction).filter(AllowanceTransaction.tx_type == "allocation").first()
        assert tx.amount == approx(750)
        assert tx.balance_after == approx(750)

    def test_duplicate_allocation_returns_existing(self, db, seed):
        """同一企业同年份重复分配返回已有配额，不重复入账。"""
        allocate_quota(db, seed["company"].id, 2025, 1000, 800, -50)
        again = allocate_quota(db, seed["company"].id, 2025, 1000, 800, -50)
        assert db.query(type(again)).count() == 1
        from app.models import AllowanceAccount

        account = db.query(AllowanceAccount).first()
        assert account.current_balance == approx(750)

    def test_clear_compliant_when_quota_sufficient(self, db, seed):
        """配额充足时清缴后状态为 compliant，缺口为 0。"""
        _add_activity(db, seed, seed["scope2"], 2025, "外购电力", 1000, "MWh")
        recalc_company_year(db, seed["company"].id, 2025)
        emission = annual_total(db, seed["company"].id, 2025)
        allocate_quota(db, seed["company"].id, 2025, baseline=1000, allocation_amount=1000, adjustment=0)
        record = clear_emission(db, seed["company"].id, 2025, "2025-12-31")
        assert record.status == "compliant"
        assert float(record.verified_emission) == approx(emission)
        assert float(record.cleared_amount) == approx(emission)
        assert float(record.deficit) == approx(0)

    def test_clear_deficit_when_quota_insufficient(self, db, seed):
        """配额不足时清缴后状态为 deficit，缺口正确。"""
        _add_activity(db, seed, seed["scope2"], 2025, "外购电力", 2000, "MWh")
        recalc_company_year(db, seed["company"].id, 2025)
        emission = annual_total(db, seed["company"].id, 2025)
        allocate_quota(db, seed["company"].id, 2025, baseline=1000, allocation_amount=800, adjustment=0)
        record = clear_emission(db, seed["company"].id, 2025, "2025-12-31")
        assert record.status == "deficit"
        assert float(record.cleared_amount) == approx(800)
        assert float(record.deficit) == approx(emission - 800)

class TestApprovalFreezeAndReversal:
    def _calculated_report(self, db, seed, qty=1000, quota=1000):
        _add_activity(db, seed, seed["scope2"], 2025, "外购电力", qty, "MWh")
        recalc_company_year(db, seed["company"].id, 2025)
        emission = annual_total(db, seed["company"].id, 2025)
        allocate_quota(db, seed["company"].id, 2025, baseline=1000, allocation_amount=quota)
        report = generate_report(db, seed["company"].id, 2025)
        submit_report(db, report)
        return emission, approve_report(db, report, verifier_id=1)

    def test_approve_freezes_allowance_and_creates_pending_compliance(self, db, seed):
        """报告批准后按排放快照冻结配额，交易可用余额减少，履约状态为待清缴。"""
        from app.models import AllowanceAccount, AllowanceTransaction, ComplianceRecord

        emission, report = self._calculated_report(db, seed, qty=1000, quota=1000)
        account = db.query(AllowanceAccount).one()
        record = db.query(ComplianceRecord).one()
        freeze_tx = db.query(AllowanceTransaction).filter(AllowanceTransaction.tx_type == "freeze").one()

        assert report.status == "approved"
        assert float(record.verified_emission) == approx(emission)
        assert float(record.frozen_amount) == approx(emission)
        assert record.status == "pending"
        assert float(account.current_balance) == approx(1000)
        assert float(account.frozen_balance) == approx(emission)
        assert float(freeze_tx.balance_after) == approx(1000)
        assert float(freeze_tx.frozen_after) == approx(emission)

    def test_frozen_allowance_cannot_be_sold(self, db, seed):
        """批准冻结的配额不能通过卖出/划出占用。"""
        from app.models import AllowanceAccount

        self._calculated_report(db, seed, qty=1000, quota=1000)
        account = db.query(AllowanceAccount).one()
        with pytest.raises(ValueError, match="可用配额"):
            transfer(db, account, 500, "sell", tx_date="2025-06-01")
        db.refresh(account)
        assert float(account.current_balance) == approx(1000)
        assert float(account.frozen_balance) == approx(570.3)

    def test_clear_consumes_frozen_quota_and_closes_compliance(self, db, seed):
        """批准后清缴核销冻结配额：持仓和冻结额同步下降，履约达标。"""
        from app.models import AllowanceAccount, ComplianceRecord

        emission, _ = self._calculated_report(db, seed, qty=1000, quota=1000)
        record = clear_emission(db, seed["company"].id, 2025, "2025-12-31")
        account = db.query(AllowanceAccount).one()

        assert record.status == "compliant"
        assert float(record.cleared_amount) == approx(emission)
        assert float(record.frozen_amount) == approx(0)
        assert float(record.deficit) == approx(0)
        assert float(account.current_balance) == approx(1000 - emission)
        assert float(account.frozen_balance) == approx(0)

    def test_partial_freeze_then_buy_topup_and_clear(self, db, seed):
        """批准时可用不足形成缺口；市场买入后补缴，冻结部分和补缴部分分别留痕。"""
        from app.models import AllowanceAccount, AllowanceTransaction, ComplianceRecord

        emission, _ = self._calculated_report(db, seed, qty=2000, quota=800)
        account = db.query(AllowanceAccount).one()
        record = db.query(ComplianceRecord).one()
        assert record.status == "deficit"
        assert float(record.frozen_amount) == approx(800)
        assert float(record.deficit) == approx(emission - 800)

        first = clear_emission(db, seed["company"].id, 2025, "2025-12-31")
        assert first.status == "deficit"
        assert float(first.cleared_amount) == approx(800)
        assert float(account.current_balance) == approx(0)

        transfer(db, account, float(first.deficit), "buy", counterparty="交易所", tx_date="2025-12-20")
        second = clear_emission(db, seed["company"].id, 2025, "2025-12-31")
        assert second.status == "compliant"
        assert float(second.cleared_amount) == approx(emission)
        assert float(second.frozen_amount) == approx(0)
        assert float(account.current_balance) == approx(0)
        assert db.query(AllowanceTransaction).filter(
            AllowanceTransaction.tx_type == "frozen_clear"
        ).count() == 1

    def test_reverse_unfreezes_and_refunds_cleared_allowance(self, db, seed):
        """批准后清缴，再冲正报告：解冻未清缴部分、退还已清缴部分并归档履约。"""
        from app.models import AllowanceAccount, ComplianceRecord, MrvReport

        emission, report = self._calculated_report(db, seed, qty=1000, quota=1000)
        clear_emission(db, seed["company"].id, 2025, "2025-12-31")
        reversed_record = reverse_report(db, report, operator_id=1, reason="核查数据有误")

        db.refresh(report)
        account = db.query(AllowanceAccount).one()
        assert report.status == "reversed"
        assert reversed_record.status == "reversed"
        assert reversed_record.is_active == 0
        assert float(reversed_record.cleared_amount) == approx(emission)
        assert float(reversed_record.frozen_amount) == approx(0)
        assert float(account.current_balance) == approx(1000)
        assert float(account.frozen_balance) == approx(0)
        assert db.query(ComplianceRecord).filter(ComplianceRecord.is_active == 1).count() == 0
        assert isinstance(report, MrvReport)

    def test_reverse_with_frozen_balance_does_not_inflate_holdings(self, db, seed):
        """批准即冻结、未清缴即冲正：只解冻不退还，持仓不变（冻结配额从未离开持仓）。

        解冻仅冻结额回落；若把冻结额也加进持仓，系统总配额会凭空增加。
        """
        from app.models import AllowanceAccount, AllowanceTransaction

        emission, report = self._calculated_report(db, seed, qty=1000, quota=1000)
        account = db.query(AllowanceAccount).one()
        assert float(account.current_balance) == approx(1000)
        assert float(account.frozen_balance) == approx(emission)

        reverse_report(db, report, operator_id=1, reason="核查数据有误")

        db.expire_all()
        account = db.query(AllowanceAccount).one()
        assert float(account.current_balance) == approx(1000)
        assert float(account.frozen_balance) == approx(0)
        unfreeze = db.query(AllowanceTransaction).filter_by(tx_type="reversal_unfreeze").one()
        assert float(unfreeze.amount) == approx(emission)
        assert float(unfreeze.balance_after) == approx(1000)
        assert float(unfreeze.frozen_after) == approx(0)
        # 无已清缴量：不产生退还流水
        assert db.query(AllowanceTransaction).filter_by(tx_type="reversal").count() == 0

    def test_reverse_failure_rolls_back_all_modules(self, db, seed, monkeypatch):
        """冲正过程中任一步失败：报告、履约、余额和流水全部回滚。"""
        from app.models import AllowanceAccount, AllowanceTransaction, ComplianceRecord, MrvReport
        from app.services import quota_service

        self._calculated_report(db, seed, qty=1000, quota=1000)

        def fail_after_refresh(*args, **kwargs):
            raise RuntimeError("模拟冲正异常")

        monkeypatch.setattr(quota_service, "_add_ledger_tx", fail_after_refresh)
        with pytest.raises(RuntimeError):
            report = db.query(MrvReport).one()
            reverse_report(db, report, operator_id=1, reason="异常回滚测试")

        db.rollback()
        db.expire_all()
        account = db.query(AllowanceAccount).one()
        report = db.query(MrvReport).one()
        record = db.query(ComplianceRecord).one()
        assert report.status == "approved"
        assert record.status == "pending"
        assert record.is_active == 1
        assert float(account.frozen_balance) == approx(570.3)
        assert db.query(AllowanceTransaction).filter(
            AllowanceTransaction.tx_type == "reversal"
        ).count() == 0

    def test_approved_report_cannot_be_regenerated_without_reversal(self, db, seed):
        """已批准报告不能被重新生成直接覆盖，必须先冲正。"""
        _, _ = self._calculated_report(db, seed)
        with pytest.raises(ValueError, match="先冲正"):
            generate_report(db, seed["company"].id, 2025)

    def test_dashboard_stats_follow_year_and_account_boundary(self, db, seed):
        """统计按年度汇总账户、冻结和履约活跃记录。"""
        from app.models import User
        from app.services.stats_service import dashboard_stats

        self._calculated_report(db, seed, qty=1000, quota=1000)
        enterprise = User(
            username="ent",
            display_name="企业用户",
            role="enterprise",
            company_id=seed["company"].id,
            password_hash="x",
            salt="x",
        )
        db.add(enterprise)
        db.commit()

        stats = dashboard_stats(db, 2025, enterprise)
        assert stats["frozen_total"] == approx(570.3)
        assert stats["current_balance_total"] == approx(1000)
        assert stats["available_total"] == approx(429.7)
        assert stats["compliance_counts"]["pending"] == 1
        assert stats["transaction_count"] >= 2
        assert dashboard_stats(db, 2024, enterprise)["frozen_total"] == approx(0)

class TestTrading:
    def test_sell_reduces_balance(self, db, seed):
        """卖出扣减余额并记录余额快照。"""
        allocate_quota(db, seed["company"].id, 2025, 1000, 800, 0)
        from app.models import AllowanceAccount

        account = db.query(AllowanceAccount).first()
        tx = transfer(db, account, 300, "sell", counterparty="某碳资产管理公司", price=80, tx_date="2025-06-01", remark="挂牌卖出")
        assert account.current_balance == approx(500)
        assert tx.balance_after == approx(500)

    def test_insufficient_balance_raises(self, db, seed):
        """余额不足时拒绝卖出且余额不变。"""
        allocate_quota(db, seed["company"].id, 2025, 1000, 800, 0)
        from app.models import AllowanceAccount

        account = db.query(AllowanceAccount).first()
        with pytest.raises(ValueError, match="余额不足"):
            transfer(db, account, 900, "sell", tx_date="2025-06-01")
        assert account.current_balance == approx(800)

    def test_buy_increases_balance(self, db, seed):
        """买入增加余额。"""
        allocate_quota(db, seed["company"].id, 2025, 1000, 800, 0)
        from app.models import AllowanceAccount

        account = db.query(AllowanceAccount).first()
        transfer(db, account, 200, "buy", counterparty="交易所", price=85, tx_date="2025-06-02", remark="大宗买入")
        assert account.current_balance == approx(1000)


class TestMrvReport:
    def test_generate_submit_approve_workflow(self, db, seed):
        """报告生成 → 提交 → 批准完整流转。"""
        _add_activity(db, seed, seed["scope2"], 2025, "外购电力", 2000, "MWh")
        recalc_company_year(db, seed["company"].id, 2025)
        total = annual_total(db, seed["company"].id, 2025)

        report = generate_report(db, seed["company"].id, 2025)
        assert report.status == "draft"
        assert float(report.total_emission) == approx(total)

        submit_report(db, report)
        assert report.status == "submitted"

        approve_report(db, report, verifier_id=1)
        assert report.status == "approved"

    def test_approve_without_submit_rejected(self, db, seed):
        """草稿不可直接批准。"""
        _add_activity(db, seed, seed["scope2"], 2025, "外购电力", 2000, "MWh")
        recalc_company_year(db, seed["company"].id, 2025)
        report = generate_report(db, seed["company"].id, 2025)
        with pytest.raises(ValueError, match="仅已提交"):
            approve_report(db, report, verifier_id=1)

    def test_approve_blocked_when_unverified_activity_exists(self, db, seed):
        """存在未核验活动数据时禁止批准报告，防止污染冻结与履约闭环。"""
        from app.models import ComplianceRecord

        _add_activity(db, seed, seed["scope2"], 2025, "外购电力", 2000, "MWh", verified=1)
        _add_activity(db, seed, seed["scope2"], 2025, "外购电力", 3000, "MWh", verified=0)
        recalc_company_year(db, seed["company"].id, 2025)
        allocate_quota(db, seed["company"].id, 2025, baseline=1000, allocation_amount=5000)
        report = generate_report(db, seed["company"].id, 2025)
        submit_report(db, report)
        with pytest.raises(ValueError, match="未核验"):
            approve_report(db, report, verifier_id=1)
        db.refresh(report)
        assert report.status == "submitted"
        assert db.query(ComplianceRecord).filter(ComplianceRecord.is_active == 1).count() == 0

    def test_approve_allowed_after_verification_and_recalculation(self, db, seed):
        """未核验数据完成核验并重新核算、重新生成报告后，批准链路恢复。"""
        pending = _add_activity(db, seed, seed["scope2"], 2025, "外购电力", 3000, "MWh", verified=0)
        _add_activity(db, seed, seed["scope2"], 2025, "外购电力", 2000, "MWh", verified=1)
        recalc_company_year(db, seed["company"].id, 2025)
        allocate_quota(db, seed["company"].id, 2025, baseline=1000, allocation_amount=5000)
        report = generate_report(db, seed["company"].id, 2025)
        submit_report(db, report)

        pending.verified = 1
        db.commit()
        recalc_company_year(db, seed["company"].id, 2025)
        report = generate_report(db, seed["company"].id, 2025)
        submit_report(db, report)
        approve_report(db, report, verifier_id=1)
        assert report.status == "approved"
        assert annual_total(db, seed["company"].id, 2025) == approx(5000 * 0.5703, rel=1e-6)

    def test_regenerate_resets_to_draft(self, db, seed):
        """重新生成报告重置为草稿。"""
        _add_activity(db, seed, seed["scope2"], 2025, "外购电力", 2000, "MWh")
        recalc_company_year(db, seed["company"].id, 2025)
        report = generate_report(db, seed["company"].id, 2025)
        submit_report(db, report)
        report = generate_report(db, seed["company"].id, 2025)
        assert report.status == "draft"
