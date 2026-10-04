"""统一账本 API：事件链查询、重放投影、检查点、对账运行与旧记录回填。

权限边界与既有监管接口一致：
- admin/verifier（监管侧）可查看全平台事件链、发起对账、回填旧记录与重建检查点；
- enterprise 仅可查看本企业相关事件与本企业/年度范围的对账结论；
  企业发起对账、查看全量运行记录、触发回填均按 403 拒绝（对账运行不写审计表，
  以 HTTP 403 为准，与 dashboard 等只读监管资源的处理保持一致）。
"""

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import ensure_company_access, get_current_user, require_roles
from app.models import AllowanceAccount, LedgerEvent, LedgerReconciliation, User
from app.services.ledger_event_service import backfill_ledger_events
from app.services.reconciliation_service import run_reconciliation, serialize_run
from app.services.replay_service import (
    rebuild_checkpoints,
    replay_account,
    replay_events_timeline,
)

router = APIRouter(prefix="/api/ledger", tags=["ledger"])

_REGULATORY = ("admin", "verifier")


def _scope_for_user(user: User, company_id: int | None) -> int | None:
    """企业用户强制把范围收敛到本企业；监管返回请求范围。"""
    if user.role == "enterprise":
        if company_id is not None and company_id != user.company_id:
            raise HTTPException(status_code=403, detail="只能查看本企业的账本数据")
        return user.company_id
    return company_id


@router.get("/events")
def list_events(
    company_id: int | None = None,
    year: int | None = None,
    account_id: int | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    before_seq: int | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """事件链时间线（分页游标 before_seq，返回按 seq 升序的一段）。

    翻页约定：把上一页首条（最小）seq 作为下一次的 ``before_seq`` 向前翻页；
    ``has_more`` 为 false 时游标不再下发。每页余额快照承接页前真实投影，
    不从零重算。
    """
    scoped_company = _scope_for_user(user, company_id)
    if account_id is not None:
        account = db.get(AllowanceAccount, account_id)
        if account is None:
            raise HTTPException(status_code=404, detail="账户不存在")
        ensure_company_access(user, account.company_id, "无权查看该账户事件")
        scoped_company = account.company_id
    timeline, has_more = replay_events_timeline(
        db, company_id=scoped_company, year=year, account_id=account_id,
        limit=limit, before_seq=before_seq,
    )
    # 向前翻页游标必须锚定本页最小 seq（升序页的首条）；没有更早事件时不再下发游标
    next_cursor = timeline[0]["seq"] if timeline and has_more else None
    return {"items": timeline, "next_before_seq": next_cursor, "has_more": has_more}


@router.get("/accounts/{account_id}/replay")
def replay_account_view(
    account_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """单账户全量重放结果：重放投影、事件数与逐笔快照不符明细。"""
    account = db.get(AllowanceAccount, account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="账户不存在")
    ensure_company_access(user, account.company_id, "无权重放该账户")
    state = replay_account(db, account_id)
    return {
        "account_id": account_id,
        "company_id": account.company_id,
        "year": account.year,
        "replayed": {
            "current_balance": state.current,
            "frozen_balance": state.frozen,
            "reserved_balance": state.reserved,
        },
        "actual": {
            "current_balance": float(account.current_balance),
            "frozen_balance": float(account.frozen_balance),
            "reserved_balance": float(account.reserved_balance),
        },
        "last_seq": state.last_seq,
        "events_applied": state.events_applied,
        "snapshot_mismatch_count": len(state.snapshot_mismatches),
        "unknown_types": sorted(state.unknown_types),
    }


@router.post("/reconcile")
def reconcile(
    request: Request,
    company_id: int | None = None,
    year: int | None = None,
    rebuild_checkpoints: bool = False,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """发起一次全量/企业/年度对账（仅监管角色；支持 Idempotency-Key 重试去重）。"""
    if user.role not in _REGULATORY:
        raise HTTPException(status_code=403, detail="仅监管角色可发起对账")
    scope = "full"
    if company_id is not None:
        scope = "company"
    elif year is not None:
        scope = "year"
    idem = request.headers.get("idempotency-key")
    run = run_reconciliation(
        db,
        scope=scope,
        company_id=company_id,
        year=year,
        idempotency_key=idem,
        triggered_by=user.id,
        rebuild_stale_checkpoints=rebuild_checkpoints,
    )
    return serialize_run(run)


@router.get("/reconciliations")
def list_reconciliations(
    status: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """对账运行历史（监管看全部；企业看本企业范围的结论概览，不含其他企业差异）。"""
    q = db.query(LedgerReconciliation)
    if user.role == "enterprise":
        q = q.filter(LedgerReconciliation.company_id == user.company_id)
    if status:
        q = q.filter(LedgerReconciliation.status == status)
    runs = q.order_by(LedgerReconciliation.id.desc()).limit(limit).all()
    result = []
    for run in runs:
        item = serialize_run(run)
        if user.role == "enterprise":
            # 企业侧不披露其他企业可能出现在全量运行摘要里的细节
            item["discrepancies"] = [
                d for d in item["discrepancies"]
                if d.get("refs", {}).get("company_id") in (None, user.company_id)
            ]
        result.append(item)
    return result


@router.get("/reconciliations/{run_id}")
def get_reconciliation(
    run_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    run = db.get(LedgerReconciliation, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="对账记录不存在")
    if user.role == "enterprise" and run.company_id not in (None, user.company_id):
        raise HTTPException(status_code=403, detail="无权查看该对账记录")
    item = serialize_run(run)
    if user.role == "enterprise":
        item["discrepancies"] = [
            d for d in item["discrepancies"]
            if d.get("refs", {}).get("company_id") in (None, user.company_id)
        ]
    return item


@router.post("/backfill")
def backfill(
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("admin")),
):
    """把旧业务表（流水/订单/竞价/履约/冲正）回填进统一事件链并重建检查点。

    仅 admin；可重复执行，只补缺不重复（事件来源唯一键 + 已回填标记）。
    """
    stats = backfill_ledger_events(db, commit=True)
    return {"status": "ok", "backfilled": stats}


@router.post("/checkpoints/rebuild")
def rebuild_all_checkpoints(
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("admin")),
):
    """全量重放并重建全部账户检查点（派生投影，可随时安全重建）。"""
    stats = rebuild_checkpoints(db, commit=True)
    return {"status": "ok", **stats}


@router.get("/chain/head")
def chain_head(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """事件链头部信息：最大 seq、总事件数（追溯链的锚点）。"""
    max_seq = db.query(LedgerEvent.seq).order_by(LedgerEvent.seq.desc()).first()
    total = db.query(LedgerEvent.id).count()
    legacy = db.query(LedgerEvent.id).filter(LedgerEvent.is_legacy == 1).count()
    return {
        "head_seq": int(max_seq[0]) if max_seq else 0,
        "total_events": total,
        "legacy_events": legacy,
        "live_events": total - legacy,
    }
