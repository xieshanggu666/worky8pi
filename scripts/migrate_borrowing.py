"""为已存在的数据库补齐“配额借贷与到期清偿”链路所需的表与列（可重复执行）。

用法：python scripts/migrate_borrowing.py

新表（仅显式创建借贷三表，存在则跳过）：
- quota_loans：企业间配额拆借借据（幂等键唯一）
- quota_loan_repayments：归还/违约收回/追偿明细（幂等键唯一）
- quota_loan_audit_logs：借贷权限与操作审计

新列：
- allowance_transactions：quota_loan_id（关联借据，PostgreSQL/MySQL 带索引）

迁移完成后自动调用统一账本回填，把旧库中已有的借据流水/借据与归还单据
状态幂等补登为 is_legacy=1 的历史事件（只补缺、零重复），并重建检查点；
与实时记账共用同一条哈希链（旧事件在前，新事件勾连其后）。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import inspect, text  # noqa: E402

from app.core.database import SessionLocal, engine  # noqa: E402
from app.models.borrowing import (  # noqa: E402
    QuotaLoan,
    QuotaLoanAuditLog,
    QuotaLoanRepayment,
)

EXPECTED_TABLES = ["quota_loans", "quota_loan_repayments", "quota_loan_audit_logs"]


def _has_column(inspector, table: str, column: str) -> bool:
    return any(c["name"] == column for c in inspector.get_columns(table))


def main():
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    new_tables: list[str] = []

    # 仅创建缺失的借贷表：显式 checkfirst=True，绝不触碰其他既有表/索引
    for model in (QuotaLoan, QuotaLoanRepayment, QuotaLoanAuditLog):
        if model.__tablename__ not in existing_tables:
            model.__table__.create(engine, checkfirst=True)
            new_tables.append(model.__tablename__)

    inspector = inspect(engine)
    statements: list[str] = []

    if "allowance_transactions" in inspector.get_table_names():
        need_index = not any(
            idx["name"] == "ix_allowance_transactions_quota_loan_id"
            for idx in inspector.get_indexes("allowance_transactions")
        )
        if not _has_column(inspector, "allowance_transactions", "quota_loan_id"):
            add_kw = "IF NOT EXISTS" if engine.dialect.name == "postgresql" else ""
            statements.append(
                f"ALTER TABLE allowance_transactions ADD COLUMN {add_kw} quota_loan_id INTEGER"
            )
            need_index = True
        if need_index:
            statements.append(
                "CREATE INDEX IF NOT EXISTS ix_allowance_transactions_quota_loan_id "
                "ON allowance_transactions (quota_loan_id)"
            )

    with engine.begin() as conn:
        for stmt in statements:
            print(f"执行：{stmt}")
            conn.execute(text(stmt))

    # 旧库借据流水/单据状态幂等回填进统一事件链并重建检查点
    from app.services.ledger_event_service import backfill_ledger_events

    db = SessionLocal()
    try:
        stats = backfill_ledger_events(db, commit=True)
    finally:
        db.close()

    if not new_tables and stats["total"] == 0:
        print("无需迁移：配额借贷表/列均已存在，统一事件链无需补登")
    else:
        if new_tables:
            print(f"新建表：{', '.join(new_tables)}")
        print(
            "统一事件链回填："
            f"流水事件 {stats['tx_events']} 笔、状态事件 {stats['status_events']} 笔、"
            f"冲正/追偿事件 {stats['reversal_events']} 笔，合计 {stats['total']} 笔；"
            "账户检查点已重建"
        )


if __name__ == "__main__":
    main()
