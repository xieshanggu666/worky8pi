"""MRV 报告：年度监测报告生成、提交、核查批准与异常冲正。"""

import json
from datetime import datetime

from sqlalchemy.orm import Session

from app.models.allowance import ComplianceRecord
from app.models.report import MrvReport
from app.services.calculation_service import annual_total, scope_totals
from app.services.quota_service import (
    freeze_allowance_for_report,
    reverse_approved_report,
)


def _build_totals(db: Session, company_id: int, year: int) -> dict:
    totals = scope_totals(db, company_id, year)
    return {
        "scope1": totals["1"],
        "scope2": totals["2"],
        "scope3": totals["3"],
        "total": round(totals["1"] + totals["2"] + totals["3"], 4),
    }


def _apply_report_detail(report: MrvReport, detail: dict, status: str = "draft") -> None:
    report.scope1 = detail["scope1"]
    report.scope2 = detail["scope2"]
    report.scope3 = detail["scope3"]
    report.total_emission = detail["total"]
    report.report_json = json.dumps(detail, ensure_ascii=False)
    report.status = status
    report.generated_at = datetime.utcnow()


def _refresh_report_inner(db: Session, company_id: int, year: int) -> tuple[str, str]:
    """在调用方事务内按最新核算结果联动报告（不自行提交）。

    返回 ``(action, warning)``：
    - ``created``/``updated``：草稿报告已按新核算结果重建；
    - ``reset_submitted``：已提交但尚未批准的报告快照已过期，退回草稿并刷新，
      须由企业重新提交（避免过期快照被批准）；
    - ``stale_submitted``：理论上不会出现（已提交即刷新）；
    - ``approved_blocked``：已批准报告受冻结快照保护，禁止改动，仅返回告警；
    - ``reversed``/``none``：无草稿需要联动。
    """
    report = db.query(MrvReport).filter(MrvReport.company_id == company_id, MrvReport.year == year).first()
    if report is None:
        return "none", ""
    if report.status == "approved":
        # 已批准报告承载排放确认、配额冻结与履约结果：任何核验联动都不得改动，
        # 须先冲正批准报告。告警供批量核验在响应中提示。
        return "approved_blocked", f"企业{company_id}{year}年度报告已批准并冻结配额，草稿未联动"
    if report.status not in ("draft", "submitted", "reversed"):
        return report.status, ""

    detail = _build_totals(db, company_id, year)
    if report.status == "submitted":
        _apply_report_detail(report, detail, status="draft")
        db.flush()
        return "reset_submitted", f"企业{company_id}{year}年度已提交报告的排放快照已随批量核验刷新，已退回草稿待重新提交"
    _apply_report_detail(report, detail, status="draft")
    db.flush()
    return ("created" if report.id is None else "updated"), ""


def refresh_draft_for_year(db: Session, company_id: int, year: int) -> tuple[str, str]:
    """批量核验后对外入口：联动刷新企业年度报告并自行提交。"""
    action, warning = _refresh_report_inner(db, company_id, year)
    db.commit()
    return action, warning


def generate_report(db: Session, company_id: int, year: int) -> MrvReport:
    """汇总年度核算结果生成 MRV 报告（已存在则重建为草稿）。

    已批准报告承载排放确认、冻结和履约结果，不能直接覆盖；确有错误时应先冲正。
    """
    report = db.query(MrvReport).filter(MrvReport.company_id == company_id, MrvReport.year == year).first()
    if report and report.status == "approved":
        raise ValueError("报告已批准并形成履约结果，请先冲正后重新生成")

    detail = _build_totals(db, company_id, year)
    if not report:
        report = MrvReport(company_id=company_id, year=year)
        db.add(report)
    _apply_report_detail(report, detail, status="draft")
    db.commit()
    db.refresh(report)
    return report


def submit_report(db: Session, report: MrvReport) -> MrvReport:
    """企业提交报告待核查。"""
    if report.status != "draft":
        raise ValueError("仅草稿状态的报告可提交")
    report.status = "submitted"
    db.commit()
    db.refresh(report)
    return report


def approve_report(db: Session, report: MrvReport, verifier_id: int) -> MrvReport:
    """核查员批准报告，并在同一事务中冻结履约配额、建立履约记录。"""
    freeze_allowance_for_report(db, report, verifier_id)
    return report


def reverse_report(db: Session, report: MrvReport, operator_id: int, reason: str) -> ComplianceRecord:
    """冲正批准报告，回滚冻结、清缴、履约状态和配额状态。"""
    return reverse_approved_report(db, report, operator_id, reason)
