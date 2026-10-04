from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import ensure_company_access, get_current_user, require_roles
from app.models import Company, EmissionScope, User
from app.schemas import CompanyIn, ScopeIn
from app.services.calculation_service import annual_total, scope_totals

router = APIRouter(prefix="/api", tags=["companies"])


def _visible_companies(db: Session, user: User):
    q = db.query(Company)
    if user.role == "enterprise":
        q = q.filter(Company.id == user.company_id)
    return q.all()


@router.get("/companies")
def list_companies(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    companies = _visible_companies(db, user)
    return [
        {
            "id": c.id,
            "code": c.code,
            "name": c.name,
            "industry": c.industry,
            "region": c.region,
            "status": c.status,
        }
        for c in companies
    ]


@router.post("/companies")
def create_company(data: CompanyIn, db: Session = Depends(get_db), user: User = Depends(require_roles("admin"))):
    if db.query(Company).filter(Company.code == data.code).first():
        raise HTTPException(status_code=400, detail="企业编号已存在")
    company = Company(
        code=data.code,
        name=data.name,
        industry=data.industry,
        region=data.region,
        boundary_desc=data.boundary_desc,
    )
    db.add(company)
    db.commit()
    db.refresh(company)
    return {"id": company.id, "code": company.code, "name": company.name}


@router.get("/companies/{company_id}")
def company_detail(company_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    company = db.get(Company, company_id)
    if not company:
        raise HTTPException(status_code=404, detail="企业不存在")
    ensure_company_access(user, company_id, "无权查看该企业")
    scopes = db.query(EmissionScope).filter(EmissionScope.company_id == company_id).all()
    return {
        "id": company.id,
        "code": company.code,
        "name": company.name,
        "industry": company.industry,
        "region": company.region,
        "boundary_desc": company.boundary_desc,
        "status": company.status,
        "scopes": [
            {"id": s.id, "scope": s.scope, "category": s.category, "name": s.name}
            for s in scopes
        ],
    }


@router.post("/companies/{company_id}/scopes")
def add_scope(company_id: int, data: ScopeIn, db: Session = Depends(get_db), user: User = Depends(require_roles("admin"))):
    company = db.get(Company, company_id)
    if not company:
        raise HTTPException(status_code=404, detail="企业不存在")
    scope = EmissionScope(company_id=company_id, scope=data.scope, category=data.category, name=data.name, description=data.description)
    db.add(scope)
    db.commit()
    db.refresh(scope)
    return {"id": scope.id, "scope": scope.scope, "name": scope.name}


@router.get("/companies/{company_id}/totals")
def company_totals(company_id: int, year: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    ensure_company_access(user, company_id, "无权查看该企业")
    totals = scope_totals(db, company_id, year)
    return {"year": year, "scopes": totals, "total": annual_total(db, company_id, year)}
