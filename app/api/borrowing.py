"""配额借贷 API：企业协商/归还、监管冻结拨付/到期标记/违约追偿与审计。

权限边界：
- 企业仅可发起/确认/撤销/归还本企业参与的借据，且只能看到本企业借据；
- 监管（admin）独占批准冻结、拨付、撤销已冻结借据、驳回、逾期标记、
  宣告违约与手动追偿；
- 审计日志仅 admin/verifier 可见；一切敏感操作与越权拒绝均写审计。
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
    QuotaLoanRejectIn,
    QuotaLoanRepayIn,
)
from app.services.borrowing_service import (
    BorrowingError,
    Operator,
    approve_loan,
    cancel_loan,
    create_loan,
    confirm_loan,
    declare_default,
    disburse_loan,
    list_audit_logs,
    list_defaulted_loans,
    list_due_loans,
    list_loans,
    list_repayments,
    mark_overdue,
    recover_loan_defaults,
    repay_loan,
    reject_loan,
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
    outstanding = round(float(loan.amount or 0) - float(loan.returned_amount or 0), 4)
    default_outstanding = round(
        float(loan.defaulted_amount or 0) - float(loan.recovered_default or 0), 4
    )
    return {
        "id": loan.id,
        "loan_no": loan.loan_no,
        "year": loan.year,
        "lender_id": loan.lender_id,
        "borrower_id": loan.borrower_id,
        "lender_name": lender.name if lender else str(loan.lender_id),
        "borrower_name": borrower.name if borrower else str(loan.borrower_id),
        "amount": float(loan.amount),
        "interest_rate": float(loan.interest_rate or 0),
        "due_date": loan.due_date,
        "status": loan.status,
        "lender_confirmed": bool(loan.lender_confirmed),
        "borrower_confirmed": bool(loan.borrower_confirmed),
        "initiator": loan.initiator,
        "returned_amount": float(loan.returned_amount or 0),
        "outstanding_principal": max(outstanding, 0.0),
        "defaulted_amount": float(loan.defaulted_amount or 0),
        "recovered_default": float(loan.recovered_default or 0),
        "default_outstanding": max(default_outstanding, 0.0),
        "auto_clear_deficit": bool(loan.auto_clear_deficit),
        "auto_recover_default": bool(loan.auto_recover_default),
        "tx_date": loan.tx_date,
        "remark": loan.remark,
        "reject_reason": loan.reject_reason,
        "cancel_reason": loan.cancel_reason,
        "confirmed_at": loan.confirmed_at,
        "reserved_at": loan.reserved_at,
        "disbursed_at": loan.disbursed_at,
        "overdue_at": loan.overdue_at,
        "defaulted_at": loan.defaulted_at,
        "closed_at": loan.closed_at,
        "cancelled_at": loan.cancelled_at,
        "rejected_at": loan.rejected_at,
        "created_at": loan.created_at,
    }


def _serialize_repayment(db: Session, r: QuotaLoanRepayment) -> dict:
    lender = db.get(Company, r.lender_id)
    borrower = db.get(Company, r.borrower_id)
    return {
        "id": r.id,
        "repay_no": r.repay_no,
        "loan_id": r.loan_id,
        "year": r.year,
        "lender_id": r.lender_id,
        "borrower_id": r.borrower_id,
        "lender_name": lender.name if lender else str(r.lender_id),
        "borrower_name": borrower.name if borrower else str(r.borrower_id),
        "quantity": float(r.quantity),
        "source": r.source,
        "operator_id": r.operator_id,
        "remark": r.remark,
        "created_at": r.created_at,
    }


# --------------------------------------------------------------------------- #
# 借据协商
# --------------------------------------------------------------------------- #

@router.get("")
def get_loans(
    year: int | None = None,
    status: str | None = None,
    role: str | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """借据列表：企业仅见本企业参与的借据；监管/核查可见全部。"""
    company_id = user.company_id if user.role == "enterprise" else None
    party_role = role if role in ("lender", "borrower") else None
    loans = list_loans(db, company_id=company_id, year=year, status=status,
                       party_role=party_role)
    return [_serialize_loan(db, loan) for loan in loans]


@router.post("")
def create(
    request: Request,
    data: QuotaLoanIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("admin", "enterprise")),
):
    # 企业只能代表本企业发起：出借方挂单则 lender_id 必须是本企业，借入方求借同理
    if user.role == "enterprise":
        own = data.borrower_id if data.initiator == "borrower" else data.lender_id
        if own != user.company_id:
            raise HTTPException(status_code=403, detail="只能以本企业名义发起配额拆借")
    idem = data.idempotency_key or request.headers.get("idempotency-key")
    try:
        loan = create_loan(
            db,
            lender_id=data.lender_id,
            borrower_id=data.borrower_id,
            year=data.year,
            amount=data.amount,
            due_date=data.due_date,
            interest_rate=data.interest_rate,
            initiator=data.initiator,
            tx_date=data.tx_date,
            remark=data.remark,
            auto_clear_deficit=data.auto_clear_deficit,
            auto_recover_default=data.auto_recover_default,
            operator=_operator(user, request),
            idempotency_key=idem,
        )
    except BorrowingError as e:
        write_audit(
            db, _operator(user, request), "loan.create",
            detail=f"拆借协商被拒绝（{data.amount} 吨）：{e}",
            result="denied", commit=True,
        )
        raise HTTPException(status_code=400, detail=str(e))
    return _serialize_loan(db, loan)


@router.get("/due")
def get_due(
    year: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """已过约定归还日但未结清的借据（企业仅本企业；监管/核查全部）。"""
    company_id = user.company_id if user.role == "enterprise" else None
    loans = list_due_loans(db, year)
    if company_id is not None:
        loans = [l for l in loans if company_id in (l.lender_id, l.borrower_id)]
    return [_serialize_loan(db, loan) for loan in loans]


@router.get("/defaults")
def get_defaults(
    year: int | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """存在违约欠额的借据：监管/核查全部；企业仅见本企业（作为借入方）的欠额。"""
    company_id = user.company_id if user.role == "enterprise" else None
    loans = list_defaulted_loans(db, borrower_id=company_id, year=year)
    return [_serialize_loan(db, loan) for loan in loans]


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
        raise HTTPException(status_code=403, detail="仅监管角色可查看审计日志")
    limit = max(1, min(limit, 500))
    logs = list_audit_logs(db, loan_id=loan_id, limit=limit)
    return [
        {
            "id": x.id,
            "operator_id": x.operator_id,
            "operator_name": x.operator_name,
            "operator_role": x.operator_role,
            "action": x.action,
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
def detail(
    loan_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    loan = db.get(QuotaLoan, loan_id)
    if not loan:
        raise HTTPException(status_code=404, detail="配额借据不存在")
    if user.role == "enterprise" and user.company_id not in (loan.lender_id, loan.borrower_id):
        raise HTTPException(status_code=403, detail="无权查看非本企业参与的借据")
    body = _serialize_loan(db, loan)
    body["repayments"] = [
        _serialize_repayment(db, r) for r in list_repayments(db, loan_id=loan_id)
    ]
    return body


@router.post("/{loan_id}/confirm")
def do_confirm(
    loan_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("admin", "enterprise")),
):
    loan = db.get(QuotaLoan, loan_id)
    if not loan:
        raise HTTPException(status_code=404, detail="配额借据不存在")
    company_id = user.company_id if user.role == "enterprise" else None
    # 监管不代企业确认；企业只能确认本企业参与的借据
    if company_id is None or company_id not in (loan.lender_id, loan.borrower_id):
        _audit_denied(
            db, user, request, "loan.confirm",
            f"{'监管账号' if company_id is None else f'企业 {company_id}'} "
            f"试图确认借据 {loan.loan_no}",
        )
        raise HTTPException(status_code=403, detail="仅借据参与方可确认")
    try:
        loan = confirm_loan(db, loan_id, company_id)
    except BorrowingError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return _serialize_loan(db, loan)


@router.post("/{loan_id}/cancel")
def do_cancel(
    loan_id: int,
    data: QuotaLoanCancelIn,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("admin", "enterprise")),
):
    """撤销借据：协商中借据参与方可撤；已冻结借据仅监管可撤（释放冻结）。"""
    loan = db.get(QuotaLoan, loan_id)
    if not loan:
        raise HTTPException(status_code=404, detail="配额借据不存在")
    as_regulator = user.role == "admin"
    if not as_regulator and user.company_id not in (loan.lender_id, loan.borrower_id):
        _audit_denied(
            db, user, request, "loan.cancel",
            f"企业 {user.company_id} 试图撤销借据 {loan.loan_no}",
        )
        raise HTTPException(status_code=403, detail="无权撤销非本企业参与的借据")
    # 已监管批准冻结的借据只能由监管撤销：企业越权尝试按 403 拒绝并留痕
    if not as_regulator and loan.status == "reserved":
        _audit_denied(
            db, user, request, "loan.cancel",
            f"企业 {user.company_id} 试图撤销已监管冻结的借据 {loan.loan_no}",
        )
        raise HTTPException(status_code=403, detail="借据已由监管批准冻结，请申请监管撤销")
    try:
        loan = cancel_loan(
            db, loan_id,
            company_id=None if as_regulator else user.company_id,
            operator=_operator(user, request),
            reason=data.reason,
        )
    except BorrowingError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return _serialize_loan(db, loan)


# --------------------------------------------------------------------------- #
# 监管动作：驳回 / 批准冻结 / 拨付 / 逾期标记 / 违约 / 追偿
# --------------------------------------------------------------------------- #

@router.post("/{loan_id}/reject")
def do_reject(
    loan_id: int,
    data: QuotaLoanRejectIn,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("admin")),
):
    try:
        loan = reject_loan(db, loan_id, _operator(user, request), data.reason)
    except BorrowingError as e:
        write_audit(
            db, _operator(user, request), "loan.reject",
            target_id=loan_id, loan_id=loan_id,
            detail=f"驳回被拒绝：{e}", result="denied", commit=True,
        )
        raise HTTPException(status_code=400, detail=str(e))
    return _serialize_loan(db, loan)


@router.post("/{loan_id}/approve")
def do_approve(loan_id: int, request: Request, db: Session = Depends(get_db),
               user: User = Depends(require_roles("admin"))):
    """监管批准借据并冻结出借方自由可用配额。"""
    try:
        loan = approve_loan(db, loan_id, _operator(user, request))
    except BorrowingError as e:
        write_audit(
            db, _operator(user, request), "loan.approve",
            target_id=loan_id, loan_id=loan_id,
            detail=f"批准冻结被拒绝：{e}", result="denied", commit=True,
        )
        raise HTTPException(status_code=400, detail=str(e))
    return _serialize_loan(db, loan)


@router.post("/{loan_id}/disburse")
def do_disburse(loan_id: int, request: Request, db: Session = Depends(get_db),
                user: User = Depends(require_roles("admin"))):
    """监管拨付：冻结配额出库、借入方到账并联动核销其履约缺口。"""
    try:
        loan = disburse_loan(db, loan_id, _operator(user, request))
    except BorrowingError as e:
        write_audit(
            db, _operator(user, request), "loan.disburse",
            target_id=loan_id, loan_id=loan_id,
            detail=f"拨付被拒绝：{e}", result="denied", commit=True,
        )
        raise HTTPException(status_code=400, detail=str(e))
    body = _serialize_loan(db, loan)
    body["repayments"] = [
        _serialize_repayment(db, r) for r in list_repayments(db, loan_id=loan_id)
    ]
    return body


@router.post("/mark-overdue")
def do_mark_overdue(
    year: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("admin")),
):
    """监管批量标记某年度已过约定归还日但未结清的借据为逾期（幂等）。"""
    marked = mark_overdue(db, year, operator=_operator(user, request))
    return {
        "marked_count": len(marked),
        "loans": [_serialize_loan(db, loan) for loan in marked],
    }


@router.post("/{loan_id}/default")
def do_default(
    loan_id: int,
    data: QuotaLoanDefaultIn,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("admin")),
):
    """监管对逾期借据宣告违约：尽力收回自由可用，不足登记违约欠额。"""
    try:
        result = declare_default(db, loan_id, _operator(user, request), data.reason)
    except BorrowingError as e:
        write_audit(
            db, _operator(user, request), "loan.default",
            target_id=loan_id, loan_id=loan_id,
            detail=f"宣告违约被拒绝：{e}", result="denied", commit=True,
        )
        raise HTTPException(status_code=400, detail=str(e))
    loan = db.get(QuotaLoan, loan_id)
    body = _serialize_loan(db, loan)
    body["clawback"] = result
    body["repayments"] = [
        _serialize_repayment(db, r) for r in list_repayments(db, loan_id=loan_id)
    ]
    return body


@router.post("/defaults/{borrower_id}/recover")
def do_recover(
    borrower_id: int,
    year: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("admin")),
):
    """监管手动追偿借入方某年度全部借据违约欠额（尽力而为）。"""
    try:
        result = recover_loan_defaults(db, borrower_id, year, _operator(user, request))
    except BorrowingError as e:
        write_audit(
            db, _operator(user, request), "loan.default.recover",
            detail=f"追偿被拒绝（企业 {borrower_id}/{year}）：{e}",
            result="denied", commit=True,
        )
        raise HTTPException(status_code=400, detail=str(e))
    return {
        "recovered_volume": float(result["recovered"]),
        "repayments": [_serialize_repayment(db, r) for r in result["repayments"]],
    }


# --------------------------------------------------------------------------- #
# 借入方归还
# --------------------------------------------------------------------------- #

@router.post("/{loan_id}/repay")
def do_repay(
    loan_id: int,
    data: QuotaLoanRepayIn,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("admin", "enterprise")),
):
    """借入方归还借据（仅借入方企业；可分次，缺省归还全额，支持幂等键）。"""
    loan = db.get(QuotaLoan, loan_id)
    if not loan:
        raise HTTPException(status_code=404, detail="配额借据不存在")
    if user.role != "enterprise" or user.company_id != loan.borrower_id:
        _audit_denied(
            db, user, request, "loan.repay",
            f"{'监管账号' if user.role != 'enterprise' else f'企业 {user.company_id}'} "
            f"试图归还借据 {loan.loan_no}",
        )
        raise HTTPException(status_code=403, detail="仅借入方企业可归还借据")
    idem = data.idempotency_key or request.headers.get("idempotency-key")
    try:
        repayment = repay_loan(
            db, loan_id, user.company_id,
            amount=data.amount, idempotency_key=idem,
            operator=_operator(user, request),
        )
    except BorrowingError as e:
        raise HTTPException(status_code=400, detail=str(e))
    loan = db.get(QuotaLoan, loan_id)
    return {
        "repayment": _serialize_repayment(db, repayment),
        "loan": _serialize_loan(db, loan),
    }
