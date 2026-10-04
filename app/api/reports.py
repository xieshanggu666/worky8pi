from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import ensure_company_access, get_current_user, require_roles
from app.models import Company, MrvReport, User
from app.schemas import ReportReversalIn
from app.services.mrv_service import generate_report, reverse_report, submit_report
from app.services.quota_service import freeze_allowance_for_report

router = APIRouter(prefix="/api", tags=["reports"])


@router.get("/companies/{company_id}/reports")
def list_reports(company_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    ensure_company_access(user, company_id, "无权查看该企业报告")
    reports = db.query(MrvReport).filter(MrvReport.company_id == company_id).order_by(MrvReport.year.desc()).all()
    return [
        {
            "id": r.id,
            "company_id": r.company_id,
            "year": r.year,
            "total_emission": float(r.total_emission),
            "scope1": float(r.scope1),
            "scope2": float(r.scope2),
            "scope3": float(r.scope3),
            "status": r.status,
            "generated_at": r.generated_at,
            "approved_at": r.approved_at,
            "reversed_at": r.reversed_at,
        }
        for r in reports
    ]


@router.post("/companies/{company_id}/reports/generate")
def generate(company_id: int, year: int, db: Session = Depends(get_db), user: User = Depends(require_roles("admin", "enterprise"))):
    company = db.get(Company, company_id)
    if not company:
        raise HTTPException(status_code=404, detail="企业不存在")
    ensure_company_access(user, company_id, "无权为该企业生成报告")
    report = generate_report(db, company_id, year)
    return {"id": report.id, "status": report.status, "total_emission": float(report.total_emission)}


@router.post("/reports/{report_id}/submit")
def submit(report_id: int, db: Session = Depends(get_db), user: User = Depends(require_roles("admin", "enterprise"))):
    report = db.get(MrvReport, report_id)
    if not report:
        raise HTTPException(status_code=404, detail="报告不存在")
    ensure_company_access(user, report.company_id, "无权提交该报告")
    try:
        submit_report(db, report)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"id": report.id, "status": report.status}


@router.post("/reports/{report_id}/approve")
def approve(report_id: int, db: Session = Depends(get_db), user: User = Depends(require_roles("verifier", "admin"))):
    report = db.get(MrvReport, report_id)
    if not report:
        raise HTTPException(status_code=404, detail="报告不存在")
    try:
        record = freeze_allowance_for_report(db, report, user.id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {
        "id": report.id,
        "status": report.status,
        "compliance_id": record.id,
        "frozen_amount": float(record.frozen_amount or 0),
        "deficit": float(record.deficit),
    }


@router.post("/reports/{report_id}/reverse")
def reverse(
    report_id: int,
    data: ReportReversalIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("verifier", "admin")),
):
    report = db.get(MrvReport, report_id)
    if not report:
        raise HTTPException(status_code=404, detail="报告不存在")
    try:
        record = reverse_report(db, report, user.id, data.reason)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {
        "id": report.id,
        "status": report.status,
        "compliance_id": record.id,
        "cleared_amount": float(record.cleared_amount),
        "frozen_amount": float(record.frozen_amount),
        "deficit": float(record.deficit),
    }


@router.get("/reports/{report_id}")
def report_detail(report_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    report = db.get(MrvReport, report_id)
    if not report:
        raise HTTPException(status_code=404, detail="报告不存在")
    ensure_company_access(user, report.company_id, "无权查看该报告")
    return {
        "id": report.id,
        "company_id": report.company_id,
        "year": report.year,
        "total_emission": float(report.total_emission),
        "scope1": float(report.scope1),
        "scope2": float(report.scope2),
        "scope3": float(report.scope3),
        "report_json": report.report_json,
        "status": report.status,
        "generated_at": report.generated_at,
        "approved_at": report.approved_at,
        "reversed_at": report.reversed_at,
        "reversal_reason": report.reversal_reason,
    }
