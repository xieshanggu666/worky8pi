"""年度活动数据批量核验与重算专项测试。

覆盖：
- 批量核验（id 列表 / 企业+年度圈定）、已核验幂等、不存在 id 报错；
- 核验 → 重算 → MRV 草稿联动在同一事务，已提交报告退回草稿、已批准报告拒绝联动；
- 批准前双重拦截：未核验数据拦截、报告快照与最新核算不一致拦截；
- 已批准年度（配额冻结）禁止再核验，配额冻结不受批量核验污染；
- 批量失败整体回滚（核验标记、核算结果、报告状态全部不变）；
- 仪表盘统计 verified/pending 随批量核验更新。
"""

import json

import pytest

from app.models import ActivityData, EmissionResult, MrvReport
from app.services.calculation_service import annual_total, recalc_company_year
from app.services.mrv_service import generate_report, submit_report
from app.services.quota_service import allocate_quota, freeze_allowance_for_report
from app.services.stats_service import dashboard_stats
from app.services.verification_service import BatchVerifyError, batch_verify_activities


def _add_activity(db, seed, scope, year, atype, qty, unit="MWh", verified=0, company=None):
    company_id = (company or seed["company"]).id
    act = ActivityData(
        company_id=company_id,
        scope_id=scope.id,
        year=year,
        period="monthly",
        activity_type=atype,
        unit=unit,
        quantity=qty,
        data_source="台账",
        recorded_by=None,
        verified=verified,
    )
    db.add(act)
    db.commit()
    db.refresh(act)
    return act


FACTOR = 0.5703


class TestBatchVerifyService:
    def test_batch_verify_by_ids_marks_and_recalcs(self, db, seed):
        """按 id 批量核验：全部置为已核验并在同事务重算排放量。"""
        a1 = _add_activity(db, seed, seed["scope2"], 2025, "外购电力", 1000)
        a2 = _add_activity(db, seed, seed["scope2"], 2025, "外购电力", 2000)
        result = batch_verify_activities(db, activity_ids=[a1.id, a2.id])
        assert result["verified_count"] == 2
        db.expire_all()
        assert db.get(ActivityData, a1.id).verified == 1
        assert db.get(ActivityData, a2.id).verified == 1
        assert annual_total(db, seed["company"].id, 2025) == pytest.approx(3000 * FACTOR, rel=1e-6)
        assert result["recalculated"][0]["result_count"] == 2
        assert result["remaining_unverified"][f"{seed['company'].id}:2025"] == 0

    def test_batch_verify_by_company_year(self, db, seed):
        """按企业+年度圈定：该企业该年度全部待核验数据一次性核验。"""
        _add_activity(db, seed, seed["scope2"], 2025, "外购电力", 1000)
        _add_activity(db, seed, seed["scope2"], 2025, "外购电力", 2000)
        _add_activity(db, seed, seed["scope2"], 2024, "外购电力", 500)
        result = batch_verify_activities(db, company_id=seed["company"].id, year=2025)
        assert result["verified_count"] == 2
        pending_2024 = (
            db.query(ActivityData).filter(ActivityData.year == 2024, ActivityData.verified == 0).count()
        )
        assert pending_2024 == 1

    def test_batch_verify_requires_year_without_ids(self, db, seed):
        with pytest.raises(BatchVerifyError):
            batch_verify_activities(db, company_id=seed["company"].id)

    def test_batch_verify_missing_id_raises_404(self, db, seed):
        a1 = _add_activity(db, seed, seed["scope2"], 2025, "外购电力", 1000)
        with pytest.raises(BatchVerifyError) as exc:
            batch_verify_activities(db, activity_ids=[a1.id, 999999])
        assert exc.value.http_status == 404

    def test_already_verified_ids_are_idempotent(self, db, seed):
        """已核验 id 重复提交不报缺失，仅收敛剩余待核验项。"""
        a1 = _add_activity(db, seed, seed["scope2"], 2025, "外购电力", 1000, verified=1)
        a2 = _add_activity(db, seed, seed["scope2"], 2025, "外购电力", 2000)
        result = batch_verify_activities(db, activity_ids=[a1.id, a2.id])
        assert result["verified_count"] == 1
        db.expire_all()
        assert db.get(ActivityData, a2.id).verified == 1

    def test_no_pending_returns_zero(self, db, seed):
        _add_activity(db, seed, seed["scope2"], 2025, "外购电力", 1000, verified=1)
        result = batch_verify_activities(db, company_id=seed["company"].id, year=2025)
        assert result["verified_count"] == 0
        assert "没有符合条件" in result["warnings"][0]

    def test_batch_verify_multi_company_year(self, db, seed):
        """跨企业或跨年度批量核验：逐企业年度重算，affected 覆盖全部组合。"""
        from app.models import Company, EmissionScope

        other = Company(code="T-002", name="测试企业乙", industry="水泥", region="测试区")
        db.add(other)
        db.flush()
        scope_o = EmissionScope(company_id=other.id, scope="2", category="外购电力", name="用电")
        db.add(scope_o)
        db.commit()
        a1 = _add_activity(db, seed, seed["scope2"], 2025, "外购电力", 1000)
        a2 = _add_activity(db, seed, scope_o, 2025, "外购电力", 2000, company=other)
        a3 = _add_activity(db, seed, seed["scope2"], 2024, "外购电力", 3000)
        result = batch_verify_activities(db, activity_ids=[a1.id, a2.id, a3.id])
        assert result["verified_count"] == 3
        pairs = {(a["company_id"], a["year"]) for a in result["affected"]}
        assert pairs == {(seed["company"].id, 2025), (other.id, 2025), (seed["company"].id, 2024)}
        assert {r["result_count"] for r in result["recalculated"]} == {1}


class TestDraftReportLinkage:
    def test_draft_refreshed_in_same_transaction(self, db, seed):
        """草稿报告在批量核验事务内随最新核算结果刷新。"""
        a = _add_activity(db, seed, seed["scope2"], 2025, "外购电力", 1000)
        recalc_company_year(db, seed["company"].id, 2025)
        report = generate_report(db, seed["company"].id, 2025)
        assert float(report.total_emission) == 0

        result = batch_verify_activities(db, activity_ids=[a.id])
        db.refresh(report)
        assert float(report.total_emission) == pytest.approx(1000 * FACTOR, rel=1e-6)
        assert report.status == "draft"
        assert result["reports"][0]["action"] == "updated"
        detail = json.loads(report.report_json)
        assert detail["scope2"] == pytest.approx(1000 * FACTOR, rel=1e-6)

    def test_submitted_report_reset_to_draft(self, db, seed):
        """已提交报告快照随核验过期：退回草稿并刷新，防止旧快照被批准。"""
        a = _add_activity(db, seed, seed["scope2"], 2025, "外购电力", 1000)
        recalc_company_year(db, seed["company"].id, 2025)
        report = generate_report(db, seed["company"].id, 2025)
        submit_report(db, report)
        assert report.status == "submitted"

        result = batch_verify_activities(db, activity_ids=[a.id])
        db.refresh(report)
        assert report.status == "draft"
        assert float(report.total_emission) == pytest.approx(1000 * FACTOR, rel=1e-6)
        assert result["reports"][0]["action"] == "reset_submitted"
        assert any("退回草稿" in w for w in result["warnings"])

    def test_approved_report_blocks_batch_verify(self, db, seed):
        """已批准年度（配额已冻结）：批量核验直接拒绝，须先冲正。"""
        _add_activity(db, seed, seed["scope2"], 2025, "外购电力", 1000, verified=1)
        pending = _add_activity(db, seed, seed["scope2"], 2025, "外购电力", 500, verified=0)
        recalc_company_year(db, seed["company"].id, 2025)
        # 注意：有未核验数据时不能批准；这里构造“核验后新增数据”的冻结场景：
        # 先全部核验、批准冻结，再补录一条待核验数据。
        pending.verified = 1
        db.commit()
        recalc_company_year(db, seed["company"].id, 2025)
        allocate_quota(db, seed["company"].id, 2025, baseline=10000, allocation_amount=10000)
        report = generate_report(db, seed["company"].id, 2025)
        submit_report(db, report)
        freeze_allowance_for_report(db, report, verifier_id=1)
        assert report.status == "approved"

        later = _add_activity(db, seed, seed["scope2"], 2025, "外购电力", 300, verified=0)
        with pytest.raises(BatchVerifyError, match="已批准"):
            batch_verify_activities(db, activity_ids=[later.id])
        db.refresh(later)
        assert later.verified == 0

    def test_no_report_still_verifies_and_recalcs(self, db, seed):
        """从未生成报告的企业年度：核验与重算照常，reports 动作是 none。"""
        a = _add_activity(db, seed, seed["scope2"], 2025, "外购电力", 1000)
        result = batch_verify_activities(db, activity_ids=[a.id])
        assert result["reports"][0]["action"] == "none"
        assert annual_total(db, seed["company"].id, 2025) == pytest.approx(1000 * FACTOR, rel=1e-6)


class TestApprovalInterception:
    def _approved_ready(self, db, seed, qty=1000, quota=10000):
        _add_activity(db, seed, seed["scope2"], 2025, "外购电力", qty, verified=1)
        recalc_company_year(db, seed["company"].id, 2025)
        allocate_quota(db, seed["company"].id, 2025, baseline=quota, allocation_amount=quota)
        report = generate_report(db, seed["company"].id, 2025)
        submit_report(db, report)
        return report

    def test_unverified_still_blocks_approval(self, db, seed):
        report = self._approved_ready(db, seed)
        _add_activity(db, seed, seed["scope2"], 2025, "外购电力", 800, verified=0)
        with pytest.raises(ValueError, match="未核验"):
            freeze_allowance_for_report(db, report, verifier_id=1)

    def test_stale_snapshot_blocks_approval(self, db, seed):
        """报告快照与最新核算结果脱节时（如核验后只重算未刷新报告）：批准被拦截。"""
        report = self._approved_ready(db, seed, qty=1000)
        # 模拟数据链路脱节：新增已核验数据后直接重算核算引擎，却未重新生成报告
        _add_activity(db, seed, seed["scope2"], 2025, "外购电力", 1000, verified=1)
        recalc_company_year(db, seed["company"].id, 2025)
        assert annual_total(db, seed["company"].id, 2025) == pytest.approx(2000 * FACTOR, rel=1e-6)
        with pytest.raises(ValueError, match="不一致"):
            freeze_allowance_for_report(db, report, verifier_id=1)
        db.refresh(report)
        assert report.status == "submitted"

        # 重新生成并提交后批准恢复，按新快照冻结
        report = generate_report(db, seed["company"].id, 2025)
        submit_report(db, report)
        freeze_allowance_for_report(db, report, verifier_id=1)
        assert report.status == "approved"

    def test_fresh_snapshot_approves(self, db, seed):
        """核验→重算→重新生成→提交→批准 全链路放行，配额按新快照冻结。"""
        report = self._approved_ready(db, seed, qty=1000)
        later = _add_activity(db, seed, seed["scope2"], 2025, "外购电力", 1000, verified=0)
        batch_verify_activities(db, activity_ids=[later.id])
        report = generate_report(db, seed["company"].id, 2025)
        submit_report(db, report)
        record = freeze_allowance_for_report(db, report, verifier_id=1)
        assert report.status == "approved"
        assert float(record.verified_emission) == pytest.approx(2000 * FACTOR, rel=1e-6)
        assert float(record.frozen_amount) == pytest.approx(2000 * FACTOR, rel=1e-6)


class TestBatchRollback:
    def test_failure_rolls_back_everything(self, db, seed, monkeypatch):
        """重算阶段抛错时整批回滚：核验标记、核算结果、报告状态全部不变。"""
        a1 = _add_activity(db, seed, seed["scope2"], 2025, "外购电力", 1000)
        a2 = _add_activity(db, seed, seed["scope2"], 2025, "外购电力", 2000)
        recalc_company_year(db, seed["company"].id, 2025)
        report = generate_report(db, seed["company"].id, 2025)
        submit_report(db, report)
        before_total = annual_total(db, seed["company"].id, 2025)

        import app.services.verification_service as vs

        def boom(*args, **kwargs):
            raise RuntimeError("重算失败模拟")

        monkeypatch.setattr(vs, "recalc_company_year", boom)
        with pytest.raises(RuntimeError):
            batch_verify_activities(db, activity_ids=[a1.id, a2.id])

        db.expire_all()
        assert db.get(ActivityData, a1.id).verified == 0
        assert db.get(ActivityData, a2.id).verified == 0
        assert annual_total(db, seed["company"].id, 2025) == before_total
        db.refresh(report)
        assert report.status == "submitted"

    def test_recalculate_false_skips_recalc(self, db, seed):
        """仅核验不重算：核算结果保持为空，并给出须重新核算的告警。"""
        a = _add_activity(db, seed, seed["scope2"], 2025, "外购电力", 1000)
        result = batch_verify_activities(db, activity_ids=[a.id], recalculate=False)
        assert result["verified_count"] == 1
        assert db.query(EmissionResult).count() == 0
        assert any("未重算" in w for w in result["warnings"])


class TestDashboardStats:
    def test_verified_pending_counts(self, db, seed):
        _add_activity(db, seed, seed["scope2"], 2025, "外购电力", 1000, verified=1)
        _add_activity(db, seed, seed["scope2"], 2025, "外购电力", 2000, verified=0)
        _add_activity(db, seed, seed["scope2"], 2025, "外购电力", 3000, verified=0)
        stats = dashboard_stats(db, year=2025)
        assert stats["total_activity"] == 3
        assert stats["verified_activity"] == 1
        assert stats["pending_activity"] == 2

        pending = db.query(ActivityData).filter(ActivityData.verified == 0).all()
        batch_verify_activities(db, activity_ids=[a.id for a in pending])
        stats = dashboard_stats(db, year=2025)
        assert stats["verified_activity"] == 3
        assert stats["pending_activity"] == 0
