"""企业间配额交易订单：挂单、双方确认、撤销与交割。

业务状态机
==========
- pending：一方挂单（发起方默认已确认），等待对方确认，此阶段不占用任何配额；
- confirmed：双方均确认，卖方账户把对应数量从“可用”转为交易占用 reserved；
- delivered：交割完成。占用配额离开卖方持仓（current/reserved 同减），
  买方持仓同额增加，双方各写一条流水；默认在同一事务内自动核销买方同年度
  履约缺口（先冻结配额、后到账配额），履约状态与配额状态同步达标；
- cancelled：交割前任一方撤销（含对方拒绝），释放卖方交易占用，状态终态。

交易占用与履约冻结的冲突处理
============================
账户同时维护 frozen_balance（履约冻结）与 reserved_balance（交易占用），
自由可用余额 = current - frozen - reserved，二者互不可挤占：

1. 确认订单时只能占用自由可用配额，报告批准冻结的履约配额不能被交易占用；
2. 报告批准冻结 / 清缴补扣时同样不能挪用已确认订单占用的交易配额；
3. 原子条件 UPDATE 在数据库层保证 ``current >= frozen + reserved``、
   frozen/reserved 各自非负，即使进程锁失效（多进程部署）也不会超额。

并发安全
========
- 订单键锁串行化同一订单的确认/撤销/交割；
- 涉及两个账户时按 ``account: < clear: < order:`` 的全局锁序取锁，杜绝死锁；
- 交割/撤销使用“状态必须为前置状态”的条件 UPDATE 抢占状态行，
  并发交割只有一个事务成功；余额、占用、流水、订单状态同一事务提交。
"""

from datetime import datetime

from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.ledger import (
    InsufficientBalanceError,
    account_lock_key,
    apply_ledger_delta,
    company_clear_key,
    is_duplicate_submit,
    lock_row_for_write,
    locked_accounts,
    trade_order_key,
    transactional,
)
from app.models.allowance import (
    AllowanceAccount,
    AllowanceTransaction,
    TradeOrder,
)
from app.models.company import Company
from app.services.loan_service import auto_recover_borrower_loans, overdue_loan_recovery_keys
from app.services.quota_service import settle_buyer_deficit_on_delivery

# 订单状态
PENDING = "pending"
CONFIRMED = "confirmed"
DELIVERED = "delivered"
CANCELLED = "cancelled"

_ACTIVE_STATUSES = (PENDING, CONFIRMED)


def _today() -> str:
    return datetime.utcnow().strftime("%Y-%m-%d")


class TradeOrderError(ValueError):
    """订单业务规则不满足（状态非法、非参与方、余额不足等）。"""


def _get_order(db: Session, order_id: int) -> TradeOrder:
    order = db.get(TradeOrder, order_id)
    if order is None:
        raise TradeOrderError("交易订单不存在")
    return order


def _require_party(order: TradeOrder, company_id: int) -> None:
    if company_id not in (order.seller_id, order.buyer_id):
        raise TradeOrderError("无权操作非本企业的交易订单")


def _get_account(db: Session, company_id: int, year: int) -> AllowanceAccount:
    account = (
        db.query(AllowanceAccount)
        .filter(AllowanceAccount.company_id == company_id, AllowanceAccount.year == year)
        .first()
    )
    if account is None:
        raise TradeOrderError(f"企业 {company_id} 的 {year} 年度配额账户不存在，请先完成配额分配")
    return account


def _company_name(db: Session, company_id: int) -> str:
    company = db.get(Company, company_id)
    return company.name if company else str(company_id)


def _lock_keys_for(
    seller_account: AllowanceAccount | None,
    buyer_account: AllowanceAccount | None,
    order_id: int,
) -> list[str]:
    """收集订单操作涉及的全部键。

    键名按字典序天然满足 ``account: < clear: < order:``，locked_accounts
    内部还会再排序一次，跨企业/跨账户操作不会形成锁环。
    """
    keys = [trade_order_key(order_id)]
    for account in (seller_account, buyer_account):
        if account is not None:
            keys.append(account_lock_key(account.id))
            keys.append(company_clear_key(account.company_id, account.year))
    return keys


def _gen_order_no(db: Session, year: int) -> str:
    count = db.query(TradeOrder).filter(TradeOrder.year == year).count()
    return f"TO{year}{count + 1:06d}"


def create_order(
    db: Session,
    seller_id: int,
    buyer_id: int,
    year: int,
    amount: float,
    price: float = 0.0,
    initiator: str = "seller",
    tx_date: str = "",
    remark: str = "",
    idempotency_key: str | None = None,
    auto_clear_deficit: bool = True,
) -> TradeOrder:
    """创建企业间交易订单（挂单）。

    挂单阶段不锁定配额；发起方默认已确认，对方确认时才占用卖方配额。
    携带相同 ``idempotency_key`` 的重复提交直接返回首笔订单。

    ``auto_clear_deficit``（默认开启）：订单交割、买方配额到账后，在同一事务内
    自动核销买方同年度履约缺口（先冻结配额、后到账可用配额），形成年度配额闭环；
    显式置为 False 时仅完成交割，买方自行决定清缴时机。
    """
    if amount <= 0:
        raise TradeOrderError("交易数量必须为正数")
    if price < 0:
        raise TradeOrderError("交易单价不能为负数")
    if initiator not in ("seller", "buyer"):
        raise TradeOrderError("发起方标识非法")
    if seller_id == buyer_id:
        raise TradeOrderError("买卖双方不能为同一企业")
    if not db.get(Company, seller_id):
        raise TradeOrderError("卖方企业不存在")
    if not db.get(Company, buyer_id):
        raise TradeOrderError("买方企业不存在")

    # 双方同年度账户必须都存在：交割时双方账户与流水都要落账
    seller_account = _get_account(db, seller_id, year)
    buyer_account = _get_account(db, buyer_id, year)

    keys = _lock_keys_for(seller_account, buyer_account, 0)
    # 建单不持有订单键（订单尚不存在）；仅取双方账户/清缴键防账户并发变更
    keys = [k for k in keys if not k.startswith("order:")]

    with locked_accounts(keys):
        if idempotency_key:
            existing = (
                db.query(TradeOrder)
                .filter(TradeOrder.idempotency_key == idempotency_key)
                .first()
            )
            if existing:
                return existing

        try:
            with transactional(db):
                order = TradeOrder(
                    order_no=_gen_order_no(db, year),
                    year=year,
                    seller_id=seller_id,
                    buyer_id=buyer_id,
                    amount=round(amount, 4),
                    price=round(price, 2),
                    status=PENDING,
                    seller_confirmed=1 if initiator == "seller" else 0,
                    buyer_confirmed=1 if initiator == "buyer" else 0,
                    initiator=initiator,
                    tx_date=tx_date or _today(),
                    remark=remark,
                    idempotency_key=idempotency_key,
                    auto_clear_deficit=1 if auto_clear_deficit else 0,
                )
                db.add(order)
                db.flush()
                db.refresh(order)
        except IntegrityError as exc:
            if idempotency_key and is_duplicate_submit(exc):
                db.rollback()
                existing = (
                    db.query(TradeOrder)
                    .filter(TradeOrder.idempotency_key == idempotency_key)
                    .first()
                )
                if existing:
                    return existing
            raise
        return order


def _add_trade_tx(
    db: Session,
    account: AllowanceAccount,
    tx_type: str,
    amount: float,
    balance_after: float,
    frozen_after: float,
    reserved_after: float,
    counterparty: str,
    remark: str,
    order: TradeOrder,
) -> AllowanceTransaction:
    tx = AllowanceTransaction(
        account_id=account.id,
        company_id=account.company_id,
        tx_type=tx_type,
        amount=round(amount, 4),
        counterparty=counterparty,
        price=round(float(order.price), 2),
        tx_date=order.tx_date or _today(),
        balance_after=round(balance_after, 4),
        frozen_after=round(frozen_after, 4),
        reserved_after=round(reserved_after, 4),
        trade_order_id=order.id,
        remark=remark,
    )
    db.add(tx)
    return tx


def confirm_order(db: Session, order_id: int, company_id: int) -> TradeOrder:
    """参与方确认订单；当双方均确认时，原子占用卖方的自由可用配额。

    - 已确认方重复确认为幂等空操作；
    - 非参与方调用按业务错误拒绝；
    - 双方确认瞬间校验卖方“current - frozen - reserved”是否足额，
      不足则拒绝（不改变任何状态/余额），等待卖方补充配额或撤销订单。
    """
    order = _get_order(db, order_id)
    _require_party(order, company_id)
    if order.status in (DELIVERED, CANCELLED):
        raise TradeOrderError("订单已交割或已撤销，不能再确认")

    seller_account = _get_account(db, order.seller_id, order.year)
    buyer_account = _get_account(db, order.buyer_id, order.year)

    with locked_accounts(_lock_keys_for(seller_account, buyer_account, order.id)):
        order = _get_order(db, order_id)
        if order.status in (DELIVERED, CANCELLED):
            raise TradeOrderError("订单已交割或已撤销，不能再确认")

        if company_id == order.seller_id:
            order.seller_confirmed = 1
        else:
            order.buyer_confirmed = 1

        # 仅单方确认：保持挂单，不占用配额
        if not (order.seller_confirmed and order.buyer_confirmed):
            with transactional(db):
                db.flush()
                db.refresh(order)
            return order

        # 双方均已确认：占用卖方自由可用配额（履约冻结配额不可被交易占用）
        if order.status == CONFIRMED:
            db.refresh(order)
            return order

        amount = round(float(order.amount), 4)
        try:
            with transactional(db):
                # 先把双方确认标志刷入数据库：后续账本 UPDATE 内部会调用 expire_all，
                # 在 autoflush=False 下若不先 flush，待写入的确认标志会被当作过期属性丢弃。
                db.flush()
                seller_account = lock_row_for_write(db, seller_account.id)
                available = round(
                    float(seller_account.current_balance)
                    - float(seller_account.frozen_balance)
                    - float(seller_account.reserved_balance),
                    4,
                )
                if available < amount:
                    raise InsufficientBalanceError("卖方可用配额不足")

                balance_after, frozen_after, reserved_after = apply_ledger_delta(
                    db, seller_account.id, 0, 0, amount
                )
                buyer_name = _company_name(db, order.buyer_id)
                _add_trade_tx(
                    db,
                    seller_account,
                    "trade_reserve",
                    amount,
                    balance_after,
                    frozen_after,
                    reserved_after,
                    buyer_name,
                    f"订单 {order.order_no} 双方确认，交易占用配额 {amount} 吨",
                    order,
                )

                order.status = CONFIRMED
                order.confirmed_at = datetime.utcnow()
                db.flush()
                db.refresh(order)
        except InsufficientBalanceError:
            # 余额不足时整笔回滚：确认标记也不保留，企业补充配额后可重新确认
            raise TradeOrderError(
                f"卖方自由可用配额不足（需 {amount} 吨，已扣除履约冻结与其他订单占用），"
                "请卖方补充配额后再确认"
            )
        return order


def _transit_status(db: Session, order_id: int, expected: tuple[str, ...], new_status: str) -> int:
    """订单状态条件 UPDATE：仅当前置状态命中时才流转，返回影响行数。

    多进程部署下进程锁无法互斥时，由数据库行更新做最后一道抢占，
    杜绝“撤销与交割并发”导致的重复释放/重复交割。
    """
    result = db.execute(
        update(TradeOrder)
        .where(TradeOrder.id == order_id)
        .where(TradeOrder.status.in_(expected))
        .values(status=new_status)
        .execution_options(synchronize_session=False)
    )
    return result.rowcount


def cancel_order(db: Session, order_id: int, company_id: int, reason: str = "") -> TradeOrder:
    """撤销订单：交割前任一参与方可撤销；confirmed 订单释放卖方占用配额。

    对已交割/已撤销订单的重复撤销调用幂等返回当前订单，不报错、不重复释放。
    """
    order = _get_order(db, order_id)
    _require_party(order, company_id)
    if order.status in (DELIVERED, CANCELLED):
        db.refresh(order)
        return order

    seller_account = _get_account(db, order.seller_id, order.year)
    buyer_account = _get_account(db, order.buyer_id, order.year)

    with locked_accounts(_lock_keys_for(seller_account, buyer_account, order.id)):
        order = _get_order(db, order_id)
        if order.status in (DELIVERED, CANCELLED):
            db.refresh(order)
            return order

        amount = round(float(order.amount), 4)
        was_confirmed = order.status == CONFIRMED
        try:
            with transactional(db):
                if _transit_status(db, order_id, _ACTIVE_STATUSES, CANCELLED) != 1:
                    # 并发下被另一事务抢先交割/撤销：整体回滚并提示
                    raise TradeOrderError("订单状态已变化，撤销失败，请刷新后重试")

                if was_confirmed:
                    lock_row_for_write(db, seller_account.id)
                    balance_after, frozen_after, reserved_after = apply_ledger_delta(
                        db, seller_account.id, 0, 0, -amount
                    )
                    buyer_name = _company_name(db, order.buyer_id)
                    _add_trade_tx(
                        db,
                        seller_account,
                        "trade_release",
                        amount,
                        balance_after,
                        frozen_after,
                        reserved_after,
                        buyer_name,
                        f"订单 {order.order_no} 撤销，释放交易占用配额 {amount} 吨",
                        order,
                    )

                order.status = CANCELLED
                order.cancelled_by = company_id
                order.cancel_reason = (reason or "").strip()[:256]
                order.cancelled_at = datetime.utcnow()
                db.flush()
                db.refresh(order)
        except InsufficientBalanceError:
            raise TradeOrderError("释放交易占用失败，账本状态异常，撤销已回滚")
        return order


def deliver_order(db: Session, order_id: int, company_id: int) -> TradeOrder:
    """交割已确认订单：占用配额划转给买方，双方账户与流水同步落账。

    卖方 current/reserved 同减（占用转为真正出库），买方 current 同增；
    任一步失败整体回滚。交割与撤销并发时由状态条件 UPDATE 保证只有一方成功。
    """
    order = _get_order(db, order_id)
    _require_party(order, company_id)
    if order.status == DELIVERED:
        db.refresh(order)
        return order
    if order.status == CANCELLED:
        raise TradeOrderError("订单已撤销，不能交割")
    if order.status != CONFIRMED:
        raise TradeOrderError("订单尚未经双方确认，不能交割")

    seller_account = _get_account(db, order.seller_id, order.year)
    buyer_account = _get_account(db, order.buyer_id, order.year)

    # 交割到账后可能自动追偿买方历史逾期/违约借贷欠额：提前把相关借贷键与
    # 受影响出借方账户键并入锁集合（键名字典序满足 account < clear < loan < order）。
    recovery_keys = overdue_loan_recovery_keys(db, order.buyer_id, order.year)
    all_keys = sorted(set(_lock_keys_for(seller_account, buyer_account, order.id) + recovery_keys))

    with locked_accounts(all_keys):
        order = _get_order(db, order_id)
        if order.status == DELIVERED:
            db.refresh(order)
            return order
        if order.status != CONFIRMED:
            raise TradeOrderError("订单未处于双方确认状态，不能交割")

        amount = round(float(order.amount), 4)
        try:
            with transactional(db):
                # 状态抢占：并发撤销/交割时只有一个事务能把 confirmed → delivered
                if _transit_status(db, order_id, (CONFIRMED,), DELIVERED) != 1:
                    raise TradeOrderError("订单状态已变化，交割失败，请刷新后重试")

                seller_account = lock_row_for_write(db, seller_account.id)
                buyer_account = lock_row_for_write(db, buyer_account.id)

                buyer_name = _company_name(db, order.buyer_id)
                seller_name = _company_name(db, order.seller_id)

                # 卖方：占用配额出库（持仓与占用同减，frozen 不变）
                s_balance, s_frozen, s_reserved = apply_ledger_delta(
                    db, seller_account.id, -amount, 0, -amount
                )
                _add_trade_tx(
                    db,
                    seller_account,
                    "trade_deliver_out",
                    amount,
                    s_balance,
                    s_frozen,
                    s_reserved,
                    buyer_name,
                    f"订单 {order.order_no} 交割，向{buyer_name}划出 {amount} 吨",
                    order,
                )

                # 买方：配额到账。买方若同时存在履约冻结，新到账配额自动补足其自由持仓，
                # 不会改动既有 frozen（买方冻结义务由清缴流程另行核销）。
                b_balance, b_frozen, b_reserved = apply_ledger_delta(
                    db, buyer_account.id, amount, 0, 0
                )
                _add_trade_tx(
                    db,
                    buyer_account,
                    "trade_deliver_in",
                    amount,
                    b_balance,
                    b_frozen,
                    b_reserved,
                    seller_name,
                    f"订单 {order.order_no} 交割，从{seller_name}受让 {amount} 吨",
                    order,
                )

                # 年度配额闭环：交割到账与买方履约缺口核销在同一事务完成。
                # 锁集合已含买方 clear/account 键，核销先消化其已冻结配额，
                # 再用刚到账的自由可用配额补缴缺口；任一步失败整笔交割回滚。
                try:
                    settle_buyer_deficit_on_delivery(db, order)
                except ValueError as exc:
                    raise TradeOrderError(f"交割联动履约核销失败，整笔交割已回滚：{exc}")

                # 先清缴后追偿：用买方清缴后的剩余自由可用自动追偿其逾期/违约借贷欠额，
                # 绝不挪用履约冻结与在途交易占用；追偿与交割同事务、同锁集合。
                auto_recover_borrower_loans(
                    db, order.buyer_id, order.year, order.tx_date or _today()
                )

                order.delivered_at = datetime.utcnow()
                db.flush()
                db.refresh(order)
        except InsufficientBalanceError:
            raise TradeOrderError("卖方交易占用状态异常，交割失败并已回滚")
        return order
