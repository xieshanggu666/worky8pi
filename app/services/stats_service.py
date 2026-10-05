"""仪表盘统计：按年度过滤，并保持企业用户的数据边界。"""

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.allowance import AllowanceAccount, AllowanceTransaction, ComplianceRecord, Quota
from app.models.company import Company
from app.models.emission import ActivityData, EmissionResult
from app.models.loan import QuotaLoan
from app.models.user import User


def dashboard_stats(db: Session, year: int | None = None, user: User | None = None) -> dict:
    company_ids = None
    if user is not None and user.role == "enterprise":
        company_ids = [user.company_id]

    def company_filtered(query, model):
        if company_ids is not None:
            query = query.filter(model.company_id.in_(company_ids))
        if year is not None:
            query = query.filter(model.year == year)
        return query

    company_query = db.query(Company)
    if company_ids is not None:
        company_query = company_query.filter(Company.id.in_(company_ids))
    total_companies = company_query.count()
    total_activity = company_filtered(db.query(ActivityData), ActivityData).count()
    pending_activity = company_filtered(db.query(ActivityData), ActivityData).filter(
        ActivityData.verified == 0
    ).count()
    verified_activity = total_activity - pending_activity
    total_results = company_filtered(db.query(EmissionResult), EmissionResult).count()

    def sum_column(model, column):
        query = db.query(func.coalesce(func.sum(column), 0)).select_from(model)
        if company_ids is not None:
            query = query.filter(model.company_id.in_(company_ids))
        if year is not None:
            query = query.filter(model.year == year)
        return query.scalar() or 0

    emission_total = sum_column(EmissionResult, EmissionResult.emission_amount)
    quota_total = sum_column(Quota, Quota.total)

    active_compliance_query = company_filtered(db.query(ComplianceRecord), ComplianceRecord).filter(
        ComplianceRecord.is_active == 1
    )
    active_records = active_compliance_query.all()
    cleared_total = sum(float(r.cleared_amount) for r in active_records)
    compliant = sum(1 for r in active_records if r.status == "compliant")
    deficit = sum(1 for r in active_records if r.status == "deficit")
    pending = sum(1 for r in active_records if r.status == "pending")

    account_query = company_filtered(db.query(AllowanceAccount), AllowanceAccount)
    current_total = sum_column(AllowanceAccount, AllowanceAccount.current_balance)
    frozen_total = sum_column(AllowanceAccount, AllowanceAccount.frozen_balance)
    reserved_total = sum_column(AllowanceAccount, AllowanceAccount.reserved_balance)

    # 配额借贷：在贷未偿（active/overdue/defaulted 的未还余额合计，企业口径含出借与借入）
    loan_query = db.query(QuotaLoan).filter(
        QuotaLoan.status.in_(["active", "overdue", "defaulted"])
    )
    if company_ids is not None:
        loan_query = loan_query.filter(
            QuotaLoan.lender_id.in_(company_ids) | QuotaLoan.borrower_id.in_(company_ids)
        )
    if year is not None:
        loan_query = loan_query.filter(QuotaLoan.year == year)
    outstanding_loan_total = sum(
        max(float(loan.amount) - float(loan.repaid_amount or 0), 0.0)
        for loan in loan_query.all()
    )
    overdue_loan_count = loan_query.filter(
        QuotaLoan.status.in_(["overdue", "defaulted"])
    ).count()

    account_id_query = db.query(AllowanceAccount.id)
    if company_ids is not None:
        account_id_query = account_id_query.filter(AllowanceAccount.company_id.in_(company_ids))
    if year is not None:
        account_id_query = account_id_query.filter(AllowanceAccount.year == year)
    account_ids = [row[0] for row in account_id_query.all()]
    tx_query = db.query(AllowanceTransaction)
    if account_ids:
        tx_query = tx_query.filter(AllowanceTransaction.account_id.in_(account_ids))
    elif company_ids is not None or year is not None:
        # 年度/企业过滤条件下没有账户时，不应回退为统计全部流水
        tx_query = tx_query.filter(AllowanceTransaction.account_id.is_(None))

    return {
        "total_companies": total_companies,
        "total_activity": total_activity,
        "verified_activity": verified_activity,
        "pending_activity": pending_activity,
        "total_results": total_results,
        "emission_total": round(float(emission_total), 4),
        "quota_total": round(float(quota_total), 4),
        "cleared_total": round(float(cleared_total), 4),
        "frozen_total": round(float(frozen_total), 4),
        "reserved_total": round(float(reserved_total), 4),
        "current_balance_total": round(float(current_total), 4),
        # 自由可用 = 持仓 - 履约冻结 - 交易占用
        "available_total": round(float(current_total) - float(frozen_total) - float(reserved_total), 4),
        "transaction_count": tx_query.count(),
        "compliance_counts": {"compliant": compliant, "deficit": deficit, "pending": pending},
        "accounts": account_query.count(),
        "outstanding_loan_total": round(float(outstanding_loan_total), 4),
        "overdue_loan_count": overdue_loan_count,
    }
