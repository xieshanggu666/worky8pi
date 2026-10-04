"""为已存在的数据库补齐“已结算竞价成交监管冲正与违约回退链路”所需的表与列。

用法：python scripts/migrate_auction_reversal.py（可重复执行）

新表（建表由 SQLAlchemy 元数据完成，存在则跳过）：
- auction_reversal_batches：监管冲正批次（幂等键唯一）
- auction_trade_reversals：逐笔（含部分）冲正明细
- auction_default_repayments：买方违约欠额补缴/追偿记录（幂等键唯一）

新列：
- auction_trades：reversed_quantity / defaulted_amount / repaid_amount
- auction_sessions：auto_recover_default（结算到账自动追偿历史违约欠额）
- allowance_transactions：tx_type 扩容到 26（新流水类型最长 26 字符）
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import inspect, text  # noqa: E402

from app.core.database import Base, engine  # noqa: E402
from app.models import (  # noqa: F401,E402
    AuctionDefaultRepayment,
    AuctionReversalBatch,
    AuctionTradeReversal,
)


def _has_column(inspector, table: str, column: str) -> bool:
    return any(c["name"] == column for c in inspector.get_columns(table))


def _add_column(statements, inspector, table: str, column: str, ddl: str) -> None:
    if table in inspector.get_table_names() and not _has_column(inspector, table, column):
        statements.append(f"ALTER TABLE {table} ADD COLUMN {ddl}")


def main():
    inspector = inspect(engine)

    # create_all 只创建缺失表，不影响已有表的数据与结构
    created_before = set(inspect(engine).get_table_names())
    Base.metadata.create_all(engine)
    created_after = set(inspect(engine).get_table_names())
    new_tables = sorted(created_after - created_before)
    inspector = inspect(engine)

    statements: list[str] = []

    if "auction_trades" in inspector.get_table_names():
        _add_column(
            statements, inspector, "auction_trades", "reversed_quantity",
            "reversed_quantity NUMERIC(18, 4) NOT NULL DEFAULT 0",
        )
        _add_column(
            statements, inspector, "auction_trades", "defaulted_amount",
            "defaulted_amount NUMERIC(18, 4) NOT NULL DEFAULT 0",
        )
        _add_column(
            statements, inspector, "auction_trades", "repaid_amount",
            "repaid_amount NUMERIC(18, 4) NOT NULL DEFAULT 0",
        )

    if "auction_sessions" in inspector.get_table_names():
        _add_column(
            statements, inspector, "auction_sessions", "auto_recover_default",
            "auto_recover_default INTEGER NOT NULL DEFAULT 1",
        )

    if "allowance_transactions" in inspector.get_table_names():
        # SQLite 无法直接改列宽，VARCHAR 本身不强制长度，无需 DDL；
        # PostgreSQL 等数据库若旧列为 VARCHAR(24) 则扩容到 26。
        if engine.dialect.name != "sqlite":
            col = next(
                (c for c in inspector.get_columns("allowance_transactions")
                 if c["name"] == "tx_type"),
                None,
            )
            if col is not None and "24" in str(getattr(col["type"], "length", "")):
                statements.append(
                    "ALTER TABLE allowance_transactions ALTER COLUMN tx_type TYPE VARCHAR(26)"
                )

    if statements:
        with engine.begin() as conn:
            for stmt in statements:
                print(f"执行：{stmt}")
                conn.execute(text(stmt))

    if not statements and not new_tables:
        print("无需迁移：冲正/违约回退链路表与列均已存在")
    else:
        if statements:
            print(f"迁移完成：{len(statements)} 项列变更")
        if new_tables:
            print(f"新建表：{', '.join(new_tables)}")


if __name__ == "__main__":
    main()
