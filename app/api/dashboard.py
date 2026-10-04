from fastapi import APIRouter, Depends

from app.core.database import get_db
from app.core.deps import get_current_user
from app.models import User
from app.services.stats_service import dashboard_stats

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])


@router.get("/stats")
def stats(year: int | None = None, db=Depends(get_db), user: User = Depends(get_current_user)):
    return dashboard_stats(db, year, user)
