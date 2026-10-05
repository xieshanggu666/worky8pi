"""配额借贷与到期清偿 API：出借/借入企业协同与监管处置。

权限边界：
- 建单：出借/借入企业以本企业名义发起（admin 不可代发，借贷是企业间行为）；
- 确认/撤销/放款/归还：仅参与企业（监管不代操作业务流转）；
- 逾期巡检/手动标记/宣布违约/监管追偿/审计：仅 admin；verifier 只读；
- 企业只能看到自己作为出借方或借入方参与的借贷单，监管侧角色可见全部；
- 一切敏感操作与越权拒绝写 quota_loan_audit_logs。
"""

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import get_current_user, require_roles
from app.models import Company, QuotaLoan, QuotaLoanRepayment, User
from app.schemas import (
    QuotaLoanCancelIn,
    QuotaLoanDefaultIn,
    QuotaLoanIn,
    QuotaLoanRepayIn,
)
from app.services.loan_service import (
    LoanError,
    Operator,
    cancel_loan,
    confirm_loan,
    create_loan,
    declare_default,
    disburse_loan,
    list_audit_logs,
    list_loans,
    list_overdue_loans,
    list_repayments,
    mark_overdue_loans,
    recover_borrower_loans,
    repay_loan,
    write_audit,
)

router = APIRouter(prefix="/api/loans", tags=["loans"])


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


def _serialize_loan(db: Session, loan: QuotaLoan) -> dict:
    lender = db.get(Company, loan.lender_id)
    borrower = db.get(Company, loan.borrower_id)
    amount = float(loan.amount)
    repaid = float(loan.repaid_amount or 0)
    outstanding = round(max(amount - repaid, 0.0), 4)
    return {
        "id": loan.id,
        "loan_no": loan.loan_no,
        "year": loan.year,
        "lender_id": loan.lender_id,
        "borrower_id": loan.borrower_id,
        "lender_name": lender.name if lender else str(loan.lender_id),
        "borrower_name": borrower.name if borrower else str(loan.borrower_id),
        "amount": amount,
        "price": float(loan.price or 0),
        "status": loan.status,
        "lender_confirmed": bool(loan.lender_confirmed),
        "borrower_confirmed": bool(loan.borrower_confirmed),
        "initiator": loan.initiator,
        "due_date": loan.due_date,
        "disbursed_amount": float(loan.disbursed_amount or 0),
        "repaid_amount": repaid,
        "outstanding_amount": outstanding,
        "defaulted_amount": float(loan.defaulted_amount or 0),
        "auto_clear_deficit": bool(loan.auto_clear_deficit),
        "auto_recover_default": bool(loan.auto_recover_default),
        "tx_date": loan.tx_date,
        "remark": loan.remark,
        "cancel_reason": loan.cancel_reason,
        "default_reason": loan.default_reason,
        "confirmed_at": loan.confirmed_at,
        "disbursed_at": loan.disbursed_at,
        "overdue_at": loan.overdue_at,
        "defaulted_at": loan.defaulted_at,
        "repaid_at": loan.repaid_at,
        "cancelled_at": loan.cancelled_at,
        "created_at": loan.created_at,
    }


def _serialize_repayment(db: Session, r: QuotaLoanRepayment) -> dict:
    return {
        "id": r.id,
        "repay_no": r.repay_no,
        "loan_id": r.loan_id,
        "lender_id": r.lender_id,
        "borrower_id": r.borrower_id,
        "year": r.year,
        "quantity": float(r.quantity),
        "kind": r.kind,
        "source": r.source,
        "operator_id": r.operator_id,
        "remark": r.remark,
        "created_at": r.created_at,
    }


def _base_query(db: Session, user: User):
    q = db.query(QuotaLoan)
    if user.role == "enterprise":
        q = q.filter(
            (QuotaLoan.lender_id == user.company_id)
            | (QuotaLoan.borrower_id == user.company_id)
        )
    return q


def _party_company_id(user: User, loan: QuotaLoan) -> int:
    """企业用户只能代表本企业；监管角色不参与业务流转（由专用监管接口处理）。"""
    if user.role != "enterprise":
        raise HTTPException(status_code=403, detail="监管账号不参与借贷业务流转，请使用企业账号")
    if user.company_id not in (loan.lender_id, loan.borrower_id):
        raise HTTPException(status_code=403, detail="无权操作该借贷单")
    return user.company_id


@router.get("")
def list_loans_api(
    year: int | None = None,
    status: str | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    q = _base_query(db, user)
    if year is not None:
        q = q.filter(QuotaLoan.year == year)
    if status:
        q = q.filter(QuotaLoan.status == status)
    items = q.order_by(QuotaLoan.id.desc()).all()
    return [_serialize_loan(db, x) for x in items]


@router.post("")
def create(
    request: Request,
    data: QuotaLoanIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("admin", "enterprise")),
):
    # 仅企业可发起借贷，且只能以本企业名义：借入求借则 borrower_id 必须是本企业
    if user.role != "enterprise":
        _audit_denied(
            db, user, request, "loan.create",
            f"{user.role} 角色试图发起配额借贷",
        )
        raise HTTPException(status_code=403, detail="仅控排企业可发起配额借贷")
    own = data.borrower_id if data.initiator == "borrower" else data.lender_id
    if own != user.company_id:
        _audit_denied(
            db, user, request, "loan.create",
            f"企业 {user.company_id} 试图以企业 {own} 名义发起借贷",
        )
        raise HTTPException(status_code=403, detail="只能以本企业名义发起配额借贷")
    idem = data.idempotency_key or request.headers.get("idempotency-key")
    try:
        loan = create_loan(
            db,
            data.lender_id,
            data.borrower_id,
            data.year,
            data.amount,
            price=data.price,
            initiator=data.initiator,
            due_date=data.due_date,
            tx_date=data.tx_date,
            remark=data.remark,
            idempotency_key=idem,
            auto_clear_deficit=data.auto_clear_deficit,
            auto_recover_default=data.auto_recover_default,
        )
    except LoanError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return _serialize_loan(db, loan)


@router.get("/overdue")
def get_overdue(
    borrower_id: int | None = None,
    year: int | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """待追偿逾期/违约借贷：监管/核查可见全部；企业仅见本企业作为借入方的欠额。"""
    company_id = user.company_id if user.role == "enterprise" else borrower_id
    loans = list_overdue_loans(db, borrower_id=company_id, year=year)
    return [_serialize_loan(db, x) for x in loans]


@router.get("/audit-logs")
def audit_logs(
    request: Request,
    loan_id: int | None = None,
    limit: int = 200,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    if user.role not in ("admin", "verifier"):
        _audit_denied(
            db, user, request, "audit.read",
            f"企业 {user.company_id} 试图读取配额借贷审计日志",
        )
        raise HTTPException(status_code=403, detail="仅监管角色可查看借贷审计日志")
    limit = max(1, min(limit, 500))
    logs = list_audit_logs(db, loan_id=loan_id, limit=limit)
    return [
        {
            "id": x.id,
            "operator_id": x.operator_id,
            "operator_name": x.operator_name,
            "operator_role": x.operator_role,
            "action": x.action,
            "target_type": x.target_type,
            "target_id": x.target_id,
            "loan_id": x.loan_id,
            "detail": x.detail,
            "result": x.result,
            "ip": x.ip,
            "created_at": x.created_at,
        }
        for x in logs
    ]


@router.get("/{loan_id}")
def detail(loan_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    loan = db.get(QuotaLoan, loan_id)
    if not loan:
        raise HTTPException(status_code=404, detail="配额借贷单不存在")
    if user.role == "enterprise" and user.company_id not in (loan.lender_id, loan.borrower_id):
        raise HTTPException(status_code=403, detail="无权查看该借贷单")
    body = _serialize_loan(db, loan)
    if user.role in ("admin", "verifier") or user.company_id in (loan.lender_id, loan.borrower_id):
        body["repayments"] = [
            _serialize_repayment(db, r) for r in list_repayments(db, loan_id=loan.id)
        ]
    return body


@router.post("/{loan_id}/confirm")
def confirm(loan_id: int, request: Request, db: Session = Depends(get_db),
            user: User = Depends(require_roles("admin", "enterprise"))):
    loan = db.get(QuotaLoan, loan_id)
    if not loan:
        raise HTTPException(status_code=404, detail="配额借贷单不存在")
    try:
        company_id = _party_company_id(user, loan)
    except HTTPException as exc:
        if exc.status_code == 403:
            _audit_denied(
                db, user, request, "loan.confirm",
                f"用户 {user.username} 试图确认借贷 {loan.loan_no}",
            )
        raise
    try:
        loan = confirm_loan(db, loan_id, company_id)
    except LoanError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return _serialize_loan(db, loan)


@router.post("/{loan_id}/cancel")
def cancel(
    loan_id: int,
    data: QuotaLoanCancelIn,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("admin", "enterprise")),
):
    loan = db.get(QuotaLoan, loan_id)
    if not loan:
        raise HTTPException(status_code=404, detail="配额借贷单不存在")
    try:
        company_id = _party_company_id(user, loan)
    except HTTPException as exc:
        if exc.status_code == 403:
            _audit_denied(
                db, user, request, "loan.cancel",
                f"用户 {user.username} 试图撤销借贷 {loan.loan_no}",
            )
        raise
    try:
        loan = cancel_loan(db, loan_id, company_id, data.reason)
    except LoanError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return _serialize_loan(db, loan)


@router.post("/{loan_id}/disburse")
def disburse(loan_id: int, request: Request, db: Session = Depends(get_db),
             user: User = Depends(require_roles("admin", "enterprise"))):
    loan = db.get(QuotaLoan, loan_id)
    if not loan:
        raise HTTPException(status_code=404, detail="配额借贷单不存在")
    try:
        company_id = _party_company_id(user, loan)
    except HTTPException as exc:
        if exc.status_code == 403:
            _audit_denied(
                db, user, request, "loan.disburse",
                f"用户 {user.username} 试图放款借贷 {loan.loan_no}",
            )
        raise
    try:
        loan = disburse_loan(db, loan_id, company_id)
    except LoanError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return _serialize_loan(db, loan)


@router.post("/{loan_id}/repay")
def repay(
    loan_id: int,
    data: QuotaLoanRepayIn,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("admin", "enterprise")),
):
    """借入方到期归还/监管手动归还（active/overdue/defaulted 均可，支持部分归还）。"""
    loan = db.get(QuotaLoan, loan_id)
    if not loan:
        raise HTTPException(status_code=404, detail="配额借贷单不存在")
    if user.role == "enterprise" and user.company_id != loan.borrower_id:
        _audit_denied(
            db, user, request, "loan.repay",
            f"企业 {user.company_id} 试图归还借入方 {loan.borrower_id} 的借贷 {loan.loan_no}",
        )
        raise HTTPException(status_code=403, detail="仅借入企业可发起归还（监管请用追偿接口）")
    if user.role != "enterprise" and user.role != "admin":
        raise HTTPException(status_code=403, detail="无权限执行该操作")
    idem = data.idempotency_key or request.headers.get("idempotency-key")
    try:
        repayment = repay_loan(
            db, loan_id, _operator(user, request),
            amount=data.amount, idempotency_key=idem,
        )
    except LoanError as e:
        raise HTTPException(status_code=400, detail=str(e))
    loan = db.get(QuotaLoan, loan_id)
    return {
        "repayment": _serialize_repayment(db, repayment),
        "loan": _serialize_loan(db, loan),
    }


# --------------------------------------------------------------------------- #
# 监管处置：逾期巡检 / 宣布违约 / 汇总追偿
# --------------------------------------------------------------------------- #

@router.post("/overdue/scan")
def scan_overdue(request: Request, db: Session = Depends(get_db),
                 user: User = Depends(require_roles("admin"))):
    """监管巡检：批量把已过到期日仍未足额归还的在贷单标记为逾期（幂等可重跑）。"""
    result = mark_overdue_loans(db, _operator(user, request))
    return {
        "marked": result["marked"],
        "loans": [_serialize_loan(db, x) for x in result["loans"]],
    }


@router.post("/{loan_id}/default")
def mark_default(
    loan_id: int,
    data: QuotaLoanDefaultIn,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("admin")),
):
    """监管对逾期借贷宣布违约，登记未偿欠额快照。"""
    try:
        loan = declare_default(db, loan_id, _operator(user, request), data.reason)
    except LoanError as e:
        write_audit(
            db, _operator(user, request), "loan.default",
            target_type="loan", target_id=loan_id, loan_id=loan_id,
            detail=f"宣布违约被拒绝：{e}", result="denied", commit=True,
        )
        raise HTTPException(status_code=400, detail=str(e))
    return _serialize_loan(db, loan)


@router.post("/defaults/{borrower_id}/recover")
def recover_borrower(
    borrower_id: int,
    year: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("admin")),
):
    """监管手动追偿借入方某年度全部逾期/违约借贷欠额（自由可用不足则尽力而为）。"""
    try:
        result = recover_borrower_loans(db, borrower_id, year, _operator(user, request))
    except LoanError as e:
        raise HTTPException(status_code=400, detail=str(e))
    loans = {r.loan_id for r in result["repayments"]}
    return {
        "recovered_volume": float(result["recovered"]),
        "repayments": [_serialize_repayment(db, r) for r in result["repayments"]],
        "loans": [_serialize_loan(db, db.get(QuotaLoan, lid)) for lid in sorted(loans)],
    }
