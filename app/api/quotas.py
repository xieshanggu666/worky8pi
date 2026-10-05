from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import ensure_company_access, get_current_user, require_roles
from app.models import AllowanceAccount, AllowanceTransaction, ComplianceRecord, Quota, User
from app.schemas import QuotaIn, TransferIn
from app.services.quota_service import allocate_quota, clear_emission
from app.services.trading_service import transfer

router = APIRouter(prefix="/api", tags=["quotas"])


@router.get("/quotas")
def list_quotas(year: int | None = None, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    q = db.query(Quota)
    if user.role == "enterprise":
        q = q.filter(Quota.company_id == user.company_id)
    if year is not None:
        q = q.filter(Quota.year == year)
    items = q.order_by(Quota.year.desc()).all()
    return [
        {
            "id": x.id,
            "company_id": x.company_id,
            "year": x.year,
            "baseline": float(x.baseline),
            "allocation_amount": float(x.allocation_amount),
            "adjustment": float(x.adjustment),
            "total": float(x.total),
            "status": x.status,
        }
        for x in items
    ]


@router.post("/quotas")
def create_quota(data: QuotaIn, db: Session = Depends(get_db), user: User = Depends(require_roles("admin"))):
    quota = allocate_quota(db, data.company_id, data.year, data.baseline, data.allocation_amount, data.adjustment)
    return {"id": quota.id, "company_id": quota.company_id, "total": float(quota.total), "status": quota.status}


@router.get("/companies/{company_id}/account")
def company_account(company_id: int, year: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    ensure_company_access(user, company_id, "无权查看该账户")
    account = (
        db.query(AllowanceAccount)
        .filter(AllowanceAccount.company_id == company_id, AllowanceAccount.year == year)
        .first()
    )
    if not account:
        raise HTTPException(status_code=404, detail="该年度尚无配额账户，请先分配配额")
    return {
        "id": account.id,
        "company_id": account.company_id,
        "year": account.year,
        "opening_balance": float(account.opening_balance),
        "current_balance": float(account.current_balance),
        "frozen_balance": float(account.frozen_balance),
        "reserved_balance": float(getattr(account, "reserved_balance", 0) or 0),
        # 自由可用 = 持仓 - 履约冻结 - 交易占用（已确认待交割订单）
        "available_balance": float(account.current_balance)
        - float(account.frozen_balance)
        - float(getattr(account, "reserved_balance", 0) or 0),
    }


@router.post("/accounts/{account_id}/transfer")
def do_transfer(request: Request, account_id: int, data: TransferIn, db: Session = Depends(get_db), user: User = Depends(require_roles("admin", "enterprise"))):
    account = db.get(AllowanceAccount, account_id)
    if not account:
        raise HTTPException(status_code=404, detail="配额账户不存在")
    ensure_company_access(user, account.company_id, "无权操作该账户")
    # 幂等键优先取请求体字段，其次取 Idempotency-Key 请求头（双击/超时重试不重复入账）
    idem_key = data.idempotency_key or request.headers.get("idempotency-key")
    try:
        tx = transfer(
            db,
            account,
            data.amount,
            data.tx_type,
            data.counterparty,
            data.price,
            data.tx_date,
            data.remark,
            idempotency_key=idem_key,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"id": tx.id, "tx_type": tx.tx_type, "amount": float(tx.amount), "balance_after": float(tx.balance_after)}


@router.get("/accounts/{account_id}/transactions")
def account_transactions(account_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    account = db.get(AllowanceAccount, account_id)
    if not account:
        raise HTTPException(status_code=404, detail="配额账户不存在")
    # 账户归属校验：控排企业只能查看本企业账户的交易明细，防止跨企业遍历 account_id 越权读取
    ensure_company_access(user, account.company_id, "无权查看该账户交易明细")
    txs = (
        db.query(AllowanceTransaction)
        .filter(
            AllowanceTransaction.account_id == account_id,
            AllowanceTransaction.company_id == account.company_id,
        )
        .order_by(AllowanceTransaction.id.asc())
        .all()
    )
    return [
        {
            "id": t.id,
            "tx_type": t.tx_type,
            "amount": float(t.amount),
            "counterparty": t.counterparty,
            "price": float(t.price) if t.price is not None else None,
            "tx_date": t.tx_date,
            "balance_after": float(t.balance_after),
            "frozen_after": float(t.frozen_after or 0),
            "reserved_after": float(getattr(t, "reserved_after", 0) or 0),
            "trade_order_id": getattr(t, "trade_order_id", None),
            "auction_trade_id": getattr(t, "auction_trade_id", None),
            "loan_id": getattr(t, "loan_id", None),
            "remark": t.remark,
        }
        for t in txs
    ]


@router.get("/compliance")
def list_compliance(year: int | None = None, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    q = db.query(ComplianceRecord)
    if user.role == "enterprise":
        q = q.filter(ComplianceRecord.company_id == user.company_id)
    q = q.filter(ComplianceRecord.is_active == 1)
    if year is not None:
        q = q.filter(ComplianceRecord.year == year)
    items = q.order_by(ComplianceRecord.year.desc()).all()
    return [
        {
            "id": r.id,
            "company_id": r.company_id,
            "year": r.year,
            "verified_emission": float(r.verified_emission),
            "cleared_amount": float(r.cleared_amount),
            "frozen_amount": float(r.frozen_amount or 0),
            "deficit": float(r.deficit),
            "status": r.status,
            "is_active": bool(r.is_active),
            "report_id": r.report_id,
            "deadline": r.deadline,
            "cleared_at": r.cleared_at,
        }
        for r in items
    ]


@router.post("/companies/{company_id}/clear")
def do_clear(
    request: Request,
    company_id: int,
    year: int,
    deadline: str,
    idempotency_key: str | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("admin")),
):
    # 幂等键优先取查询参数，其次取 Idempotency-Key 请求头；重复清缴请求返回首次结果
    idem_key = idempotency_key or request.headers.get("idempotency-key")
    try:
        record = clear_emission(db, company_id, year, deadline, idempotency_key=idem_key)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {
        "id": record.id,
        "status": record.status,
        "verified_emission": float(record.verified_emission),
        "cleared_amount": float(record.cleared_amount),
        "frozen_amount": float(record.frozen_amount or 0),
        "deficit": float(record.deficit),
    }
