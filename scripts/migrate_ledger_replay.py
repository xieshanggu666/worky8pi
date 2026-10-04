"""统一账本重放/对账链路迁移：建表 + 旧记录回填 + 检查点初始化（可重复执行）。

用法：python scripts/migrate_ledger_replay.py

新增 3 张表（由 SQLAlchemy 元数据创建，存在则跳过）：
- ledger_events：统一事件流（append-only，seq 全序 + 链式哈希）
- ledger_checkpoints：账户重放投影检查点
- ledger_reconciliations：对账运行记录

回填范围（五类业务的旧记录全部补登为 is_legacy=1 的历史事件）：
- allowance_transactions：每笔流水（携带原始三余额快照）
- trade_orders / auction_sessions / auction_bids / auction_trades：状态时点
- compliance_records / mrv_reports：履约与报告生命周期
- auction_trade_reversals / auction_default_repayments：部分冲正与违约追偿

回填幂等：事件 (source, source_ref, occurrence) 唯一，重复执行只补缺；
回填完成后全量重放一次并重建检查点。回填本身不改动任何业务表数据。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import inspect  # noqa: E402

from app.core.database import Base, SessionLocal, engine  # noqa: E402
from app.models import (  # noqa: F401,E402
    LedgerCheckpoint,
    LedgerEvent,
    LedgerReconciliation,
)
from app.services.ledger_event_service import backfill_ledger_events  # noqa: E402

EXPECTED_TABLES = {"ledger_events", "ledger_checkpoints", "ledger_reconciliations"}


def main():
    inspector = inspect(engine)
    created_before = set(inspect(engine).get_table_names())
    Base.metadata.create_all(engine)
    inspector = inspect(engine)
    created_after = set(inspect(engine).get_table_names())
    new_tables = sorted(EXPECTED_TABLES & (created_after - created_before))
    missing = sorted(EXPECTED_TABLES - created_after)
    if missing:  # 理论不可达：create_all 后应齐全
        print(f"警告：以下账本表仍缺失：{', '.join(missing)}")

    db = SessionLocal()
    try:
        stats = backfill_ledger_events(db, commit=True)
    finally:
        db.close()

    if new_tables:
        print(f"新建表：{', '.join(new_tables)}")
    if stats["total"]:
        print(
            "旧记录回填完成："
            f"流水事件 {stats['tx_events']} 笔、状态事件 {stats['status_events']} 笔、"
            f"冲正/追偿事件 {stats['reversal_events']} 笔，合计 {stats['total']} 笔；"
            "账户检查点已重建"
        )
    else:
        print("无需回填：全部旧记录均已在统一事件链中（检查点已校准）")


if __name__ == "__main__":
    main()
