"""碳配额集中竞价市场 API：监管（场次/撮合/结算/审计）与买/卖方（报价/撤单/成交查询）。

权限边界：
- 场次管理（建场/开放/撮合/结算/撤场）仅 admin；verifier 只读；
- 企业仅可为本企业报价/撤单，仅可查看本企业参与的成交；
- 审计日志仅 admin/verifier 可见；一切敏感操作与越权拒绝均写审计。
"""

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import get_current_user, require_roles
from app.models import (
    AuctionBid,
    AuctionDefaultRepayment,
    AuctionReversalBatch,
    AuctionSession,
    AuctionTrade,
    AuctionTradeReversal,
    Company,
    User,
)
from app.schemas import (
    AuctionBidCancelIn,
    AuctionBidIn,
    AuctionDefaultRepayIn,
    AuctionSessionCancelIn,
    AuctionSessionIn,
    AuctionTradeReversalIn,
)
from app.services.auction_service import (
    AuctionError,
    Operator,
    cancel_bid,
    cancel_session,
    create_session,
    list_audit_logs,
    list_bids,
    list_default_repayments,
    list_defaulted_trades,
    list_reversal_batches,
    list_sessions,
    list_trade_reversals,
    list_trades,
    open_session,
    place_bid,
    recover_buyer_defaults,
    repay_trade_default,
    reverse_settled_trades,
    run_matching,
    settle_session,
    write_audit,
)

router = APIRouter(prefix="/api/auctions", tags=["auctions"])


def _operator(user: User, request: Request) -> Operator:
    return Operator(
        id=user.id,
        username=user.username,
        role=user.role,
        ip=(request.client.host if request.client else "") or "",
    )


def _audit_denied(db: Session, user: User, request: Request, action: str, detail: str) -> None:
    """越权拒绝即时落审计（无业务事务，独立提交）。"""
    write_audit(
        db,
        _operator(user, request),
        action,
        detail=detail,
        result="denied",
        commit=True,
    )


def _serialize_session(db: Session, s: AuctionSession) -> dict:
    creator = db.get(User, s.created_by) if s.created_by else None
    bids = list_bids(db, session_id=s.id)
    active_bids = [b for b in bids if b.status == "active"]
    return {
        "id": s.id,
        "session_no": s.session_no,
        "name": s.name,
        "year": s.year,
        "product": s.product,
        "reserve_price": float(s.reserve_price or 0),
        "estimated_volume": float(s.estimated_volume) if s.estimated_volume is not None else None,
        "status": s.status,
        "clear_price": float(s.clear_price) if s.clear_price is not None else None,
        "matched_volume": float(s.matched_volume or 0),
        "trade_count": s.trade_count or 0,
        "bid_count": len(active_bids),
        "auto_clear_deficit": bool(s.auto_clear_deficit),
        "auto_recover_default": bool(getattr(s, "auto_recover_default", 1)),
        "open_at": s.open_at,
        "close_at": s.close_at,
        "matched_at": s.matched_at,
        "settled_at": s.settled_at,
        "cancelled_at": s.cancelled_at,
        "cancel_reason": s.cancel_reason,
        "created_by": s.created_by,
        "created_by_name": creator.display_name if creator else (creator.username if creator else ""),
        "remark": s.remark,
        "created_at": s.created_at,
    }


def _serialize_bid(db: Session, b: AuctionBid) -> dict:
    company = db.get(Company, b.company_id)
    return {
        "id": b.id,
        "bid_no": b.bid_no,
        "session_id": b.session_id,
        "company_id": b.company_id,
        "company_name": company.name if company else str(b.company_id),
        "side": b.side,
        "year": b.year,
        "quantity": float(b.quantity),
        "price": float(b.price),
        "filled_quantity": float(b.filled_quantity or 0),
        "status": b.status,
        "tx_date": b.tx_date,
        "remark": b.remark,
        "cancel_reason": b.cancel_reason,
        "matched_at": b.matched_at,
        "cancelled_at": b.cancelled_at,
        "created_at": b.created_at,
    }


def _serialize_trade(db: Session, t: AuctionTrade) -> dict:
    buyer = db.get(Company, t.buyer_id)
    seller = db.get(Company, t.seller_id)
    defaulted_outstanding = round(
        float(t.defaulted_amount or 0) - float(t.repaid_amount or 0), 4
    )
    return {
        "id": t.id,
        "trade_no": t.trade_no,
        "session_id": t.session_id,
        "buyer_bid_id": t.buyer_bid_id,
        "seller_bid_id": t.seller_bid_id,
        "buyer_id": t.buyer_id,
        "seller_id": t.seller_id,
        "buyer_name": buyer.name if buyer else str(t.buyer_id),
        "seller_name": seller.name if seller else str(t.seller_id),
        "year": t.year,
        "quantity": float(t.quantity),
        "price": float(t.price),
        "alloc_seq": t.alloc_seq,
        "status": t.status,
        "settled_at": t.settled_at,
        "cancelled_at": t.cancelled_at,
        "reversed_quantity": float(t.reversed_quantity or 0),
        "defaulted_amount": float(t.defaulted_amount or 0),
        "repaid_amount": float(t.repaid_amount or 0),
        "default_outstanding": max(defaulted_outstanding, 0.0),
        "created_at": t.created_at,
    }


def _serialize_batch(db: Session, b: AuctionReversalBatch) -> dict:
    return {
        "id": b.id,
        "batch_no": b.batch_no,
        "session_id": b.session_id,
        "reason": b.reason,
        "operator_id": b.operator_id,
        "trade_count": b.trade_count,
        "reverse_volume": float(b.reverse_volume or 0),
        "recovered_volume": float(b.recovered_volume or 0),
        "default_volume": float(b.default_volume or 0),
        "created_at": b.created_at,
    }


def _serialize_reversal(db: Session, r: AuctionTradeReversal) -> dict:
    return {
        "id": r.id,
        "reversal_no": r.reversal_no,
        "batch_id": r.batch_id,
        "session_id": r.session_id,
        "trade_id": r.trade_id,
        "buyer_id": r.buyer_id,
        "seller_id": r.seller_id,
        "year": r.year,
        "quantity": float(r.quantity),
        "clear_unfrozen": float(r.clear_unfrozen or 0),
        "clear_refunded": float(r.clear_refunded or 0),
        "recovered_quantity": float(r.recovered_quantity or 0),
        "defaulted_quantity": float(r.defaulted_quantity or 0),
        "reason": r.reason,
        "created_at": r.created_at,
    }


def _serialize_repayment(db: Session, p: AuctionDefaultRepayment) -> dict:
    return {
        "id": p.id,
        "repay_no": p.repay_no,
        "session_id": p.session_id,
        "trade_id": p.trade_id,
        "reversal_id": p.reversal_id,
        "buyer_id": p.buyer_id,
        "seller_id": p.seller_id,
        "year": p.year,
        "quantity": float(p.quantity),
        "source": p.source,
        "operator_id": p.operator_id,
        "remark": p.remark,
        "created_at": p.created_at,
    }


# --------------------------------------------------------------------------- #
# 场次
# --------------------------------------------------------------------------- #

@router.get("")
def get_sessions(
    year: int | None = None,
    status: str | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    return [_serialize_session(db, s) for s in list_sessions(db, year=year, status=status)]


@router.get("/my-trades")
def my_trades(
    session_id: int | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """企业仅见本企业参与的成交；监管/核查角色可见全部。"""
    company_id = user.company_id if user.role == "enterprise" else None
    trades = list_trades(db, session_id=session_id, company_id=company_id)
    return [_serialize_trade(db, t) for t in trades]


@router.get("/audit-logs")
def audit_logs(
    request: Request,
    session_id: int | None = None,
    limit: int = 200,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    if user.role not in ("admin", "verifier"):
        _audit_denied(
            db, user, request, "audit.read",
            f"企业 {user.company_id} 试图读取竞价审计日志",
        )
        raise HTTPException(status_code=403, detail="仅监管角色可查看审计日志")
    limit = max(1, min(limit, 500))
    logs = list_audit_logs(db, session_id=session_id, limit=limit)
    return [
        {
            "id": x.id,
            "operator_id": x.operator_id,
            "operator_name": x.operator_name,
            "operator_role": x.operator_role,
            "action": x.action,
            "target_type": x.target_type,
            "target_id": x.target_id,
            "session_id": x.session_id,
            "detail": x.detail,
            "result": x.result,
            "ip": x.ip,
            "created_at": x.created_at,
        }
        for x in logs
    ]


@router.post("")
def create(
    request: Request,
    data: AuctionSessionIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("admin")),
):
    idem = data.idempotency_key or request.headers.get("idempotency-key")
    try:
        session = create_session(
            db,
            year=data.year,
            name=data.name,
            reserve_price=data.reserve_price,
            estimated_volume=data.estimated_volume,
            product=data.product,
            auto_clear_deficit=data.auto_clear_deficit,
            auto_recover_default=data.auto_recover_default,
            remark=data.remark,
            open_at=data.open_at,
            close_at=data.close_at,
            operator=_operator(user, request),
            idempotency_key=idem,
        )
    except AuctionError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return _serialize_session(db, session)


@router.get("/defaults")
def get_defaults(
    buyer_id: int | None = None,
    year: int | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """待追偿违约成交单：监管/核查可见全部；企业仅见本企业（作为买方）的欠额。"""
    company_id = user.company_id if user.role == "enterprise" else buyer_id
    trades = list_defaulted_trades(db, buyer_id=company_id, year=year)
    return [_serialize_trade(db, t) for t in trades]


@router.get("/reversals")
def get_reversals(
    request: Request,
    session_id: int | None = None,
    trade_id: int | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """冲正批次、逐笔冲正单与违约补缴记录；仅监管/核查可见，企业越权读取 403 并留痕。"""
    if user.role not in ("admin", "verifier"):
        _audit_denied(
            db, user, request, "audit.read",
            f"企业 {user.company_id} 试图读取竞价冲正记录",
        )
        raise HTTPException(status_code=403, detail="仅监管角色可查看冲正记录")
    batches = list_reversal_batches(db, session_id=session_id)
    details = list_trade_reversals(db, session_id=session_id, trade_id=trade_id)
    repayments = list_default_repayments(db, session_id=session_id, trade_id=trade_id)
    return {
        "batches": [_serialize_batch(db, b) for b in batches],
        "reversals": [_serialize_reversal(db, r) for r in details],
        "repayments": [_serialize_repayment(db, p) for p in repayments],
    }


@router.get("/{session_id}")
def detail(session_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    session = db.get(AuctionSession, session_id)
    if not session:
        raise HTTPException(status_code=404, detail="竞价场次不存在")
    return _serialize_session(db, session)


@router.post("/{session_id}/open")
def do_open(session_id: int, request: Request, db: Session = Depends(get_db),
            user: User = Depends(require_roles("admin"))):
    try:
        session = open_session(db, session_id, _operator(user, request))
    except AuctionError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return _serialize_session(db, session)


@router.post("/{session_id}/match")
def do_match(session_id: int, request: Request, db: Session = Depends(get_db),
             user: User = Depends(require_roles("admin"))):
    try:
        session = run_matching(db, session_id, _operator(user, request))
    except AuctionError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return _serialize_session(db, session)


@router.post("/{session_id}/settle")
def do_settle(session_id: int, request: Request, db: Session = Depends(get_db),
              user: User = Depends(require_roles("admin"))):
    try:
        session = settle_session(db, session_id, _operator(user, request))
    except AuctionError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return _serialize_session(db, session)


@router.post("/{session_id}/cancel")
def do_cancel(
    session_id: int,
    data: AuctionSessionCancelIn,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("admin")),
):
    try:
        session = cancel_session(db, session_id, _operator(user, request), data.reason)
    except AuctionError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return _serialize_session(db, session)


# --------------------------------------------------------------------------- #
# 已结算成交单：监管冲正 / 违约追偿（POST 动作路径与场次动作同构）
# --------------------------------------------------------------------------- #

@router.post("/{session_id}/reverse")
def do_reverse(
    session_id: int,
    data: AuctionTradeReversalIn,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("admin")),
):
    """监管冲正已结算成交单：双方配额回退、联动清缴回滚、不足登记违约欠额。"""
    idem = data.idempotency_key or request.headers.get("idempotency-key")
    try:
        batch = reverse_settled_trades(
            db,
            session_id,
            _operator(user, request),
            data.reason,
            trade_ids=data.trade_ids,
            quantities=data.quantities,
            idempotency_key=idem,
        )
    except AuctionError as e:
        write_audit(
            db, _operator(user, request), "session.reverse",
            target_type="session", session_id=session_id,
            detail=f"冲正被拒绝：{e}", result="denied", commit=True,
        )
        raise HTTPException(status_code=400, detail=str(e))
    body = _serialize_batch(db, batch)
    body["reversals"] = [
        _serialize_reversal(db, r)
        for r in list_trade_reversals(db, session_id=session_id)
        if r.batch_id == batch.id
    ]
    return body


@router.post("/defaults/{buyer_id}/recover")
def do_recover_buyer(
    buyer_id: int,
    year: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("admin")),
):
    """监管手动追偿买方某年度全部违约欠额（自由可用不足则尽力而为）。"""
    try:
        result = recover_buyer_defaults(db, buyer_id, year, _operator(user, request))
    except AuctionError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {
        "recovered_volume": float(result["recovered"]),
        "repayments": [_serialize_repayment(db, p) for p in result["repayments"]],
    }


@router.post("/trades/{trade_id}/repay")
def do_repay_trade(
    trade_id: int,
    data: AuctionDefaultRepayIn,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("admin")),
):
    """监管对单笔违约成交单手动追偿。"""
    idem = data.idempotency_key or request.headers.get("idempotency-key")
    try:
        repayment = repay_trade_default(
            db, trade_id, _operator(user, request),
            amount=data.amount, idempotency_key=idem,
        )
    except AuctionError as e:
        write_audit(
            db, _operator(user, request), "trade.default.recover",
            target_type="trade", target_id=trade_id,
            detail=f"追偿被拒绝：{e}", result="denied", commit=True,
        )
        raise HTTPException(status_code=400, detail=str(e))
    trade = db.get(AuctionTrade, repayment.trade_id)
    return {
        "repayment": _serialize_repayment(db, repayment),
        "trade": _serialize_trade(db, trade),
    }


# --------------------------------------------------------------------------- #
# 报价
# --------------------------------------------------------------------------- #

@router.get("/{session_id}/bids")
def get_bids(
    session_id: int,
    status_filter: str | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    if not db.get(AuctionSession, session_id):
        raise HTTPException(status_code=404, detail="竞价场次不存在")
    company_id = user.company_id if user.role == "enterprise" else None
    bids = list_bids(db, session_id=session_id, company_id=company_id, status=status_filter)
    return [_serialize_bid(db, b) for b in bids]


@router.post("/{session_id}/bids")
def place(
    session_id: int,
    data: AuctionBidIn,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("admin", "enterprise")),
):
    # 企业只能为本企业报价；监管代企业报价不从此入口开放（企业自主密封报价）
    if user.role == "enterprise":
        company_id = user.company_id
    else:
        raise HTTPException(status_code=403, detail="监管账号不参与报价，请使用企业账号")
    idem = data.idempotency_key or request.headers.get("idempotency-key")
    try:
        bid = place_bid(
            db,
            session_id,
            company_id,
            data.side,
            data.quantity,
            data.price,
            tx_date=data.tx_date,
            remark=data.remark,
            operator=_operator(user, request),
            idempotency_key=idem,
        )
    except AuctionError as e:
        # 业务拒绝（余额不足/重复报价/非开放状态）也留痕，便于监管审计异常报价行为
        write_audit(
            db, _operator(user, request), "bid.place",
            target_type="session", session_id=session_id,
            detail=f"报价被拒绝（{data.side} {data.quantity} 吨 @ {data.price}）：{e}",
            result="denied", commit=True,
        )
        raise HTTPException(status_code=400, detail=str(e))
    return _serialize_bid(db, bid)


@router.post("/bids/{bid_id}/cancel")
def bid_cancel(
    bid_id: int,
    data: AuctionBidCancelIn,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("admin", "enterprise")),
):
    bid = db.get(AuctionBid, bid_id)
    if not bid:
        raise HTTPException(status_code=404, detail="报价单不存在")
    as_regulator = user.role == "admin"
    if not as_regulator and user.company_id != bid.company_id:
        _audit_denied(
            db, user, request, "bid.cancel",
            f"企业 {user.company_id} 试图撤销企业 {bid.company_id} 的报价 {bid.bid_no}",
        )
        raise HTTPException(status_code=403, detail="无权撤销其他企业的报价")
    try:
        bid = cancel_bid(
            db, bid_id,
            user.company_id if not as_regulator else None,
            _operator(user, request),
            data.reason,
            as_regulator=as_regulator,
        )
    except AuctionError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return _serialize_bid(db, bid)


# --------------------------------------------------------------------------- #
# 成交
# --------------------------------------------------------------------------- #

@router.get("/trades/all")
def all_trades(
    request: Request,
    session_id: int | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """监管/核查侧查看全部成交；企业请用 /api/auctions/my-trades。"""
    if user.role == "enterprise":
        _audit_denied(
            db, user, request, "trade.read",
            f"企业 {user.company_id} 试图读取全市场成交明细",
        )
        raise HTTPException(status_code=403, detail="企业仅可查看本企业参与的成交：/api/auctions/my-trades")
    trades = list_trades(db, session_id=session_id)
    return [_serialize_trade(db, t) for t in trades]
