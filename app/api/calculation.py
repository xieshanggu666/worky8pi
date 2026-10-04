from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import ensure_company_access, get_current_user, require_roles
from app.models import Company, EmissionResult, User
from app.services.calculation_service import (
    count_unverified_activities,
    recalc_company_year,
    scope_totals,
)

router = APIRouter(prefix="/api", tags=["calculation"])


@router.post("/companies/{company_id}/calculate")
def calculate(company_id: int, year: int, db: Session = Depends(get_db), user: User = Depends(require_roles("admin", "enterprise"))):
    company = db.get(Company, company_id)
    if not company:
        raise HTTPException(status_code=404, detail="企业不存在")
    ensure_company_access(user, company_id, "无权为该企业核算")
    count = recalc_company_year(db, company_id, year)
    unverified_count = count_unverified_activities(db, company_id, year)
    totals = scope_totals(db, company_id, year)
    warning = ""
    if unverified_count:
        warning = f"有 {unverified_count} 条活动数据尚未核验，未纳入本次核算；核验通过后请重新核算"
    return {
        "count": count,
        "unverified_count": unverified_count,
        "warning": warning,
        "totals": totals,
        "total": round(sum(totals.values()), 4),
    }


@router.get("/companies/{company_id}/results")
def list_results(company_id: int, year: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    ensure_company_access(user, company_id, "无权查看该企业")
    results = (
        db.query(EmissionResult)
        .filter(EmissionResult.company_id == company_id, EmissionResult.year == year)
        .order_by(EmissionResult.id.desc())
        .all()
    )
    return [
        {
            "id": r.id,
            "activity_id": r.activity_id,
            "method_code": r.method_code,
            "activity_quantity": float(r.activity_quantity),
            "factor_value": float(r.factor_value),
            "emission_amount": float(r.emission_amount),
        }
        for r in results
    ]
