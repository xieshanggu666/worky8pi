"""为已存在的数据库补齐“配额借贷与到期清偿链路”所需的表与列。

用法：python scripts/migrate_quota_loans.py（可重复执行）

新表（建表由 SQLAlchemy 元数据完成，存在则跳过）：
- quota_loans：配额借贷单（申请/确认/冻结/放款/归还/逾期/违约状态机，幂等键唯一）
- quota_loan_repayments：归还/违约追偿凭据（幂等键唯一）
- quota_loan_audit_logs：借贷权限与操作审计

新列：
- allowance_transactions：loan_id（关联借贷单）
- ledger_events：loan_id（统一账本事件关联借贷单）

迁移完成后自动把旧库中的借贷业务记录幂等回填进统一事件链（可重复执行，
实时记账库上只补缺、零重复），并重建账户检查点。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import inspect, text  # noqa: E402

from app.core.database import Base, SessionLocal, engine  # noqa: E402
import app.models  # noqa: F401,E402  确保全部表（含借贷三表）注册到元数据
from app.services.ledger_event_service import backfill_ledger_events  # noqa: E402

EXPECTED_TABLES = {"quota_loans", "quota_loan_repayments", "quota_loan_audit_logs"}


def _has_column(inspector, table: str, column: str) -> bool:
    return any(c["name"] == column for c in inspector.get_columns(table))


def main():
    inspector = inspect(engine)
    created_before = set(inspect(engine).get_table_names())
    Base.metadata.create_all(engine)
    inspector = inspect(engine)
    created_after = set(inspect(engine).get_table_names())
    new_tables = sorted(EXPECTED_TABLES & (created_after - created_before))

    statements: list[str] = []
    for table, column, ddl in (
        ("allowance_transactions", "loan_id",
         "loan_id INTEGER"),
        ("ledger_events", "loan_id",
         "loan_id INTEGER"),
    ):
        if table in inspector.get_table_names() and not _has_column(inspector, table, column):
            statements.append(f"ALTER TABLE {table} ADD COLUMN {ddl}")
            statements.append(f"CREATE INDEX IF NOT EXISTS ix_{table}_{column} ON {table} ({column})")

    if statements:
        with engine.begin() as conn:
            for stmt in statements:
                print(f"执行：{stmt}")
                conn.execute(text(stmt))

    # 回填借贷业务记录（流水/状态/归还凭据）进统一事件链并重建检查点；幂等可重跑
    db = SessionLocal()
    try:
        stats = backfill_ledger_events(db, commit=True)
    finally:
        db.close()

    if not statements and not new_tables:
        print("无需迁移：配额借贷链路表与列均已存在")
    else:
        if new_tables:
            print(f"新建表：{', '.join(new_tables)}")
        if statements:
            print(f"迁移完成：{len(statements)} 项列/索引变更")
    print(
        f"事件链回填：新增事件合计 {stats['total']} 笔"
        "（已有借贷/其他业务事件不重复登记），账户检查点已重建"
    )


if __name__ == "__main__":
    main()
