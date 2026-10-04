from datetime import date

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import get_current_user, require_roles
from app.models import EmissionFactor, FactorVersion, User
from app.schemas import FactorIn

router = APIRouter(prefix="/api", tags=["factors"])


@router.get("/factors")
def list_factors(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    factors = db.query(EmissionFactor).order_by(EmissionFactor.factor_code.asc()).all()
    return [
        {
            "id": f.id,
            "factor_code": f.factor_code,
            "name": f.name,
            "scope": f.scope,
            "unit": f.unit,
            "value": float(f.value),
            "source": f.source,
            "valid_from": f.valid_from,
            "valid_to": f.valid_to,
        }
        for f in factors
    ]


@router.post("/factors")
def create_factor(data: FactorIn, db: Session = Depends(get_db), user: User = Depends(require_roles("admin"))):
    if db.query(EmissionFactor).filter(EmissionFactor.factor_code == data.factor_code).first():
        raise HTTPException(status_code=400, detail="因子编号已存在")
    factor = EmissionFactor(
        factor_code=data.factor_code,
        name=data.name,
        scope=data.scope,
        unit=data.unit,
        value=data.value,
        source=data.source,
        valid_from=data.valid_from or date.today().isoformat(),
        valid_to=data.valid_to,
    )
    db.add(factor)
    db.add(FactorVersion(factor_id=factor.id, version_no=1, value=data.value, valid_from=factor.valid_from, note="初始版本"))
    db.commit()
    db.refresh(factor)
    return {"id": factor.id, "factor_code": factor.factor_code}


@router.put("/factors/{factor_id}")
def update_factor(factor_id: int, data: FactorIn, db: Session = Depends(get_db), user: User = Depends(require_roles("admin"))):
    factor = db.get(EmissionFactor, factor_id)
    if not factor:
        raise HTTPException(status_code=404, detail="因子不存在")
    factor.name = data.name
    factor.scope = data.scope
    factor.unit = data.unit
    factor.value = data.value
    factor.source = data.source
    factor.valid_from = data.valid_from or factor.valid_from
    factor.valid_to = data.valid_to
    last_version = db.query(FactorVersion).filter(FactorVersion.factor_id == factor_id).order_by(FactorVersion.version_no.desc()).first()
    version_no = (last_version.version_no + 1) if last_version else 1
    db.add(FactorVersion(factor_id=factor_id, version_no=version_no, value=data.value, valid_from=factor.valid_from, note="因子修订"))
    db.commit()
    return {"id": factor.id, "version_no": version_no, "value": float(factor.value)}
