"""企业间交易订单 API：挂单、查询、双方确认、撤销与交割。"""

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import get_current_user, require_roles
from app.models import Company, ComplianceRecord, TradeOrder, User
from app.schemas import TradeOrderCancelIn, TradeOrderIn
from app.services.trade_order_service import (
    TradeOrderError,
    cancel_order,
    confirm_order,
    create_order,
    deliver_order,
)

router = APIRouter(prefix="/api/trade-orders", tags=["trade-orders"])


def _serialize(db: Session, order: TradeOrder) -> dict:
    seller = db.get(Company, order.seller_id)
    buyer = db.get(Company, order.buyer_id)
    return {
        "id": order.id,
        "order_no": order.order_no,
        "year": order.year,
        "seller_id": order.seller_id,
        "buyer_id": order.buyer_id,
        "seller_name": seller.name if seller else str(order.seller_id),
        "buyer_name": buyer.name if buyer else str(order.buyer_id),
        "amount": float(order.amount),
        "price": float(order.price or 0),
        "status": order.status,
        "seller_confirmed": bool(order.seller_confirmed),
        "buyer_confirmed": bool(order.buyer_confirmed),
        "initiator": order.initiator,
        "auto_clear_deficit": bool(getattr(order, "auto_clear_deficit", 1)),
        "tx_date": order.tx_date,
        "remark": order.remark,
        "cancel_reason": order.cancel_reason,
        "confirmed_at": order.confirmed_at,
        "delivered_at": order.delivered_at,
        "cancelled_at": order.cancelled_at,
        "created_at": order.created_at,
    }


def _buyer_clearance(db: Session, order: TradeOrder) -> dict | None:
    """交割响应附带的买方履约核销结果（无活跃履约记录则为 None）。"""
    record = (
        db.query(ComplianceRecord)
        .filter(
            ComplianceRecord.company_id == order.buyer_id,
            ComplianceRecord.year == order.year,
            ComplianceRecord.is_active == 1,
        )
        .first()
    )
    if record is None:
        return None
    return {
        "id": record.id,
        "status": record.status,
        "verified_emission": float(record.verified_emission),
        "cleared_amount": float(record.cleared_amount),
        "frozen_amount": float(record.frozen_amount or 0),
        "deficit": float(record.deficit),
    }


def _base_query(db: Session, user: User):
    q = db.query(TradeOrder)
    # 企业只能看到自己作为买方或卖方的订单，监管侧角色可查看全部
    if user.role == "enterprise":
        q = q.filter(
            (TradeOrder.seller_id == user.company_id) | (TradeOrder.buyer_id == user.company_id)
        )
    return q


@router.get("")
def list_orders(
    year: int | None = None,
    status: str | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    q = _base_query(db, user)
    if year is not None:
        q = q.filter(TradeOrder.year == year)
    if status:
        q = q.filter(TradeOrder.status == status)
    items = q.order_by(TradeOrder.id.desc()).all()
    return [_serialize(db, o) for o in items]


@router.post("")
def create(
    request: Request,
    data: TradeOrderIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("admin", "enterprise")),
):
    # 企业挂单只能代表本企业：买方求购则 buyer_id 必须是本企业，卖方挂单同理
    if user.role == "enterprise":
        own = data.buyer_id if data.initiator == "buyer" else data.seller_id
        if own != user.company_id:
            raise HTTPException(status_code=403, detail="只能以本企业名义发起交易订单")
    idem_key = data.idempotency_key or request.headers.get("idempotency-key")
    try:
        order = create_order(
            db,
            data.seller_id,
            data.buyer_id,
            data.year,
            data.amount,
            data.price,
            data.initiator,
            data.tx_date,
            data.remark,
            idempotency_key=idem_key,
            auto_clear_deficit=data.auto_clear_deficit,
        )
    except TradeOrderError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return _serialize(db, order)


@router.get("/{order_id}")
def detail(order_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    order = db.get(TradeOrder, order_id)
    if not order:
        raise HTTPException(status_code=404, detail="交易订单不存在")
    if user.role == "enterprise" and user.company_id not in (order.seller_id, order.buyer_id):
        raise HTTPException(status_code=403, detail="无权查看该交易订单")
    return _serialize(db, order)


def _party_company_id(user: User, order: TradeOrder) -> int:
    """企业用户只能代表本企业；监管角色代为操作时按订单卖方落操作人。"""
    if user.role == "enterprise":
        if user.company_id not in (order.seller_id, order.buyer_id):
            raise HTTPException(status_code=403, detail="无权操作该交易订单")
        return user.company_id
    return order.seller_id


@router.post("/{order_id}/confirm")
def confirm(order_id: int, db: Session = Depends(get_db), user: User = Depends(require_roles("admin", "enterprise"))):
    order = db.get(TradeOrder, order_id)
    if not order:
        raise HTTPException(status_code=404, detail="交易订单不存在")
    company_id = _party_company_id(user, order)
    try:
        order = confirm_order(db, order_id, company_id)
    except TradeOrderError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return _serialize(db, order)


@router.post("/{order_id}/cancel")
def cancel(
    order_id: int,
    data: TradeOrderCancelIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("admin", "enterprise")),
):
    order = db.get(TradeOrder, order_id)
    if not order:
        raise HTTPException(status_code=404, detail="交易订单不存在")
    company_id = _party_company_id(user, order)
    try:
        order = cancel_order(db, order_id, company_id, data.reason)
    except TradeOrderError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return _serialize(db, order)


@router.post("/{order_id}/deliver")
def deliver(order_id: int, db: Session = Depends(get_db), user: User = Depends(require_roles("admin", "enterprise"))):
    order = db.get(TradeOrder, order_id)
    if not order:
        raise HTTPException(status_code=404, detail="交易订单不存在")
    company_id = _party_company_id(user, order)
    try:
        order = deliver_order(db, order_id, company_id)
    except TradeOrderError as e:
        raise HTTPException(status_code=400, detail=str(e))
    payload = _serialize(db, order)
    payload["buyer_clearance"] = _buyer_clearance(db, order)
    return payload
