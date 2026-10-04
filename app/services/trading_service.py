"""配额交易台账：买入/卖出/划转。

并发安全保证：
- 账户级键锁串行化同一账户的所有余额变更；
- 扣减使用“可用余额（持仓 - 冻结）充足”的原子 UPDATE，冻结配额不可卖出；
- 余额与流水在同一事务中提交，异常统一回滚；
- 支持幂等键，重复提交（双击、网络重试）返回首笔流水，不重复入账。
"""

from sqlalchemy.orm import Session

from app.core.ledger import (
    InsufficientBalanceError,
    account_lock_key,
    apply_ledger_delta,
    is_duplicate_submit,
    lock_row_for_write,
    locked_accounts,
    transactional,
)
from app.models.allowance import AllowanceAccount, AllowanceTransaction

_INCREASE_TYPES = {"buy", "transfer_in"}
_DECREASE_TYPES = {"sell", "transfer_out"}


def transfer(
    db: Session,
    account: AllowanceAccount,
    amount: float,
    tx_type: str,
    counterparty: str = "",
    price: float | None = None,
    tx_date: str = "",
    remark: str = "",
    idempotency_key: str | None = None,
) -> AllowanceTransaction:
    """在配额账户上划转配额，校验可用余额后写流水。

    同一 ``account`` 携带相同 ``idempotency_key`` 的重复调用直接返回首次流水，
    不会二次扣减/入账。任何失败都整体回滚，余额与流水保持一致。
    """
    if amount <= 0:
        raise ValueError("划转数量必须为正数")
    if tx_type not in _INCREASE_TYPES | _DECREASE_TYPES:
        raise ValueError(f"不支持的交易类型: {tx_type}")

    key = account_lock_key(account.id)
    with locked_accounts([key]):
        # 幂等命中：首笔请求已成功，直接返回，不重复履约/扣减
        if idempotency_key:
            existing = (
                db.query(AllowanceTransaction)
                .filter(
                    AllowanceTransaction.account_id == account.id,
                    AllowanceTransaction.idempotency_key == idempotency_key,
                )
                .first()
            )
            if existing:
                return existing

        try:
            with transactional(db):
                # 抢占账户行写锁后读到的可用余额在提交前不会被并发清缴/订单改动
                account = lock_row_for_write(db, account.id)
                delta = amount if tx_type in _INCREASE_TYPES else -amount
                # 原子条件 UPDATE：最终余额、冻结额与占用额由数据库计算。卖出/划出只能使用
                # current - frozen - reserved 的自由可用部分；报告批准冻结的履约配额与
                # 已确认订单占用的交易配额均不得被重复卖出。
                if delta < 0:
                    available = (
                        float(account.current_balance)
                        - float(account.frozen_balance)
                        - float(account.reserved_balance)
                    )
                    if round(available, 4) < amount:
                        raise InsufficientBalanceError("可用配额余额不足")
                balance_after, frozen_after, reserved_after = apply_ledger_delta(
                    db, account.id, delta, 0, 0
                )

                tx = AllowanceTransaction(
                    account_id=account.id,
                    company_id=account.company_id,
                    tx_type=tx_type,
                    amount=round(amount, 4),
                    counterparty=counterparty,
                    price=round(price, 2) if price is not None else None,
                    tx_date=tx_date,
                    balance_after=balance_after,
                    frozen_after=frozen_after,
                    reserved_after=reserved_after,
                    remark=remark,
                    idempotency_key=idempotency_key,
                )
                db.add(tx)
                db.flush()
                # 同步会话内对象，调用方读取 account.current_balance 即为最新值
                db.refresh(account)
                db.refresh(tx)
        except InsufficientBalanceError:
            raise ValueError("可用配额余额不足")
        except Exception as exc:
            # 与并发的首笔请求撞幂等键时回滚并返回首笔流水，视为重复提交
            if idempotency_key and is_duplicate_submit(exc):
                db.rollback()
                existing = (
                    db.query(AllowanceTransaction)
                    .filter(
                        AllowanceTransaction.account_id == account.id,
                        AllowanceTransaction.idempotency_key == idempotency_key,
                    )
                    .first()
                )
                if existing:
                    return existing
            raise
        return tx
