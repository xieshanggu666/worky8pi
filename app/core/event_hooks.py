"""SQLAlchemy 会话事件钩子：流水与统一账本事件自动同事务登记。

选择会话级 ``after_flush`` 而非在四个业务服务里逐个埋点的原因：

- 配额流水散落在 quota_service / trading_service / trade_order_service /
  auction_service 的二十余处写入点，手工埋点必然有遗漏风险；
- ``after_flush`` 在同一事务、同一数据库连接内触发，此时新流水已拿到主键，
  据此构造的 :class:`LedgerEvent` 在同一轮 flush 内被工作单元顺带落库
  （SQLAlchemy 官方 versioned-history 同款机制）——流水与事件同生共死，
  业务回滚时事件 INSERT 随之回滚，不会出现“有事件无流水”；
- 回填（``is_legacy=1``）走显式批量插入，不经本钩子，避免双重登记。

序号分配与保存点细节见 :mod:`app.services.ledger_event_service`。
"""

from __future__ import annotations

import logging

from sqlalchemy import event, inspect
from sqlalchemy.orm import Session

from app.models.allowance import AllowanceTransaction
from app.services.ledger_event_service import record_event_for_tx

logger = logging.getLogger(__name__)

_INSTALLED_FLAG = "_carbon_ledger_hook_installed"


def install_ledger_hooks(session_factory) -> None:
    """在会话工厂上安装 after_flush / 事务结束钩子（幂等，重复调用安全）。"""
    if getattr(session_factory, _INSTALLED_FLAG, False):
        return

    @event.listens_for(session_factory, "after_flush")
    def _record_ledger_events(session: Session, flush_context) -> None:  # noqa: ANN001
        # 仅处理本工作单元内新插入、且已经 flush 拿到主键的流水。
        # 此时不能再显式 flush（会重入 flush 状态机）；事件只需 session.add，
        # 工作单元会在同一轮 flush 的后续 INSERT 通道内把它们落库。
        txs = [obj for obj in session.new if isinstance(obj, AllowanceTransaction)]
        if not txs:
            return
        # session.new 是无序集合：同一事务同一账户多笔流水（如结算循环中
        # 同一买方连续到账+补缴）必须按流水主键（即业务发生顺序）登记事件，
        # 重放时逐笔余额快照才能与落账快照一一对齐。
        txs.sort(key=lambda t: t.id if t.id is not None else 0)
        for tx in txs:
            if tx.id is None:
                # 极端情况下个别对象尚未取号，交由下一轮 flush 补登
                continue
            try:
                record_event_for_tx(session, tx)
            except Exception:  # pragma: no cover - 登记失败不可静默吞掉
                logger.exception("统一账本事件登记失败（流水 id=%s）", tx.id)
                raise

    @event.listens_for(session_factory, "after_rollback")
    def _reset_seq_after_rollback(session: Session) -> None:
        # 事务结束后本会话的 seq 续号缓存作废，下次从库内 MAX(seq) 重新取号
        session.info.pop("ledger_event_seq", None)

    @event.listens_for(session_factory, "after_commit")
    def _reset_seq_after_commit(session: Session) -> None:
        session.info.pop("ledger_event_seq", None)

    setattr(session_factory, _INSTALLED_FLAG, True)


def is_session_persistent(obj) -> bool:
    """对象是否已在会话中有持久态主键（工具函数）。"""
    return inspect(obj).persistent
