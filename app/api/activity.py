from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import ensure_company_access, get_current_user, require_roles
from app.models import ActivityData, EmissionScope, User
from app.schemas import ActivityBatchVerifyIn, ActivityIn
from app.services.verification_service import BatchVerifyError, batch_verify_activities

router = APIRouter(prefix="/api", tags=["activity"])


@router.get("/activity")
def list_activity(
    company_id: int | None = None,
    year: int | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    q = db.query(ActivityData)
    if user.role == "enterprise":
        q = q.filter(ActivityData.company_id == user.company_id)
    elif company_id is not None:
        q = q.filter(ActivityData.company_id == company_id)
    if year is not None:
        q = q.filter(ActivityData.year == year)
    items = q.order_by(ActivityData.record_date.desc()).all()
    scope_map = {s.id: s.scope for s in db.query(EmissionScope).all()}
    return [
        {
            "id": a.id,
            "company_id": a.company_id,
            "scope_id": a.scope_id,
            "scope_no": scope_map.get(a.scope_id, ""),
            "year": a.year,
            "period": a.period,
            "activity_type": a.activity_type,
            "unit": a.unit,
            "quantity": float(a.quantity),
            "data_source": a.data_source,
            "verified": a.verified,
            "record_date": a.record_date,
        }
        for a in items
    ]


@router.post("/activity")
def create_activity(data: ActivityIn, db: Session = Depends(get_db), user: User = Depends(require_roles("enterprise", "admin"))):
    scope = db.get(EmissionScope, data.scope_id)
    if not scope:
        raise HTTPException(status_code=404, detail="核算边界不存在")
    if user.role == "enterprise":
        ensure_company_access(user, scope.company_id, "无权为该企业录入数据")
        company_id = user.company_id
    else:
        company_id = scope.company_id
    activity = ActivityData(
        company_id=company_id,
        scope_id=data.scope_id,
        year=data.year,
        period=data.period,
        activity_type=data.activity_type,
        unit=data.unit,
        quantity=data.quantity,
        data_source=data.data_source,
        recorded_by=user.id,
    )
    db.add(activity)
    db.commit()
    db.refresh(activity)
    return {"id": activity.id, "quantity": float(activity.quantity)}


@router.post("/activity/batch-verify")
def batch_verify(
    data: ActivityBatchVerifyIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("verifier", "admin")),
):
    """批量核验活动数据：按 id 列表或 企业+年度 圈定，核验→重算→草稿联动单事务完成。

    - 已批准年度（配额已冻结）拒绝核验，须先冲正报告；
    - 已提交报告快照随核验重算刷新并退回草稿，避免过期快照被批准；
    - 任何一步失败整批回滚，不会出现部分核验成功。
    """
    ids = data.activity_ids or None
    try:
        result = batch_verify_activities(
            db,
            activity_ids=ids,
            company_id=data.company_id,
            year=data.year,
            recalculate=data.recalculate,
        )
    except BatchVerifyError as e:
        raise HTTPException(status_code=e.http_status, detail=str(e))
    return result


@router.post("/activity/{activity_id}/verify")
def verify_activity(activity_id: int, db: Session = Depends(get_db), user: User = Depends(require_roles("verifier", "admin"))):
    """单条核验：走批量核验内核，同样联动重算与 MRV 草稿（单事务可回滚）。"""
    activity = db.get(ActivityData, activity_id)
    if not activity:
        raise HTTPException(status_code=404, detail="活动数据不存在")
    try:
        result = batch_verify_activities(db, activity_ids=[activity_id], recalculate=True)
    except BatchVerifyError as e:
        raise HTTPException(status_code=e.http_status, detail=str(e))
    affected = result["affected"][0] if result["affected"] else {}
    return {
        "id": activity_id,
        "verified": 1,
        "verified_count": result["verified_count"],
        "recalculated": result["recalculated"],
        "reports": result["reports"],
        "warnings": result["warnings"],
        "company_id": affected.get("company_id"),
        "year": affected.get("year"),
    }
