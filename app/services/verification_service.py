"""活动数据批量核验与重算。

从逐条核验升级为批量操作，并打通年度配额闭环：

1. **批量核验**：按 id 列表或「企业 + 年度」圈定待核验数据，统一置为 verified=1；
2. **批量重算**：核验完成后在**同一事务**内对受影响的每个企业年度先清后算，
   未核验数据继续在源头排除；
3. **MRV 草稿联动**：同事务内按新核算结果刷新草稿报告，已提交报告快照过期则
   退回草稿待重新提交，已批准报告受冻结快照保护禁止改动；
4. **批准拦截**：批准前既有“未核验数据”拦截，也有“报告快照与最新核算不一致”拦截；
5. **配额冻结**：只允许最新核算快照进入批准冻结链路，批准年度的数据核验被拒绝；
6. **仪表盘统计**：返回值含各企业年度剩余未核验条数，供前端与看板联动。

“核验标记 → 重算结果 → 报告草稿”全部在单一事务提交，任何一步失败均整体回滚，
绝不出现“已核验却未重算”或“结果已变而报告仍旧”的半成品状态。
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.models.company import Company
from app.models.emission import ActivityData, EmissionResult
from app.models.report import MrvReport
from app.services.calculation_service import recalc_company_year
from app.services.mrv_service import _refresh_report_inner


class BatchVerifyError(ValueError):
    """批量核验业务错误，http_status 给出对应的 HTTP 状态码。"""

    def __init__(self, message: str, http_status: int = 400):
        super().__init__(message)
        self.http_status = http_status


def _select_pending(
    db: Session,
    activity_ids: list[int] | None,
    company_id: int | None,
    year: int | None,
) -> list[ActivityData]:
    """按 id 列表或企业+年度圈定**待核验**（verified=0）活动数据。"""
    q = db.query(ActivityData).filter(ActivityData.verified == 0)
    if activity_ids:
        activities = q.filter(ActivityData.id.in_(activity_ids)).order_by(ActivityData.id).all()
        found = {a.id for a in activities}
        missing = sorted(set(activity_ids) - found)
        if missing:
            # 显式指定的 id 不存在，或已经核验过（不在 verified=0 集合中）：
            # 已核验视为幂等收敛，仅不存在的 id 报错。
            existing = (
                db.query(ActivityData.id).filter(ActivityData.id.in_(missing)).all()
            )
            really_missing = sorted(set(missing) - {row[0] for row in existing})
            if really_missing:
                raise BatchVerifyError(
                    f"活动数据不存在：{', '.join(map(str, really_missing[:20]))}",
                    http_status=404,
                )
        return activities

    if year is None:
        raise BatchVerifyError("批量核验需提供 activity_ids，或提供 year（可再带 company_id）")
    if company_id is not None:
        q = q.filter(ActivityData.company_id == company_id)
    q = q.filter(ActivityData.year == year)
    return q.order_by(ActivityData.id).all()


def _company_names(db: Session, company_ids: set[int]) -> dict[int, str]:
    if not company_ids:
        return {}
    rows = db.query(Company.id, Company.name).filter(Company.id.in_(company_ids)).all()
    return {cid: name for cid, name in rows}


def _guard_approved_years(db: Session, pairs: set[tuple[int, int]], names: dict[int, str]) -> None:
    """批准年度拦截：已批准 MRV 报告承载冻结快照，禁止再核验/重算该年度数据。

    数据核验后重算会改变排放量，而批准报告以快照形式锁定了履约义务和配额冻结；
    允许改动将造成“活动台账/核算结果”与“履约冻结”不一致。须先冲正批准报告。
    """
    blocked: list[str] = []
    for company_id, year in sorted(pairs):
        approved = (
            db.query(MrvReport.id)
            .filter(
                MrvReport.company_id == company_id,
                MrvReport.year == year,
                MrvReport.status == "approved",
            )
            .first()
        )
        if approved:
            blocked.append(f"{names.get(company_id, f'企业{company_id}')}{year}年度")
    if blocked:
        raise BatchVerifyError(
            "以下年度报告已批准并冻结配额，不能再核验活动数据；请先冲正批准报告后重试："
            + "、".join(blocked)
        )


def batch_verify_activities(
    db: Session,
    activity_ids: list[int] | None = None,
    company_id: int | None = None,
    year: int | None = None,
    recalculate: bool = True,
) -> dict:
    """批量核验活动数据并联动重算与 MRV 草稿（单事务，失败整体回滚）。

    返回汇总：核验条数、受影响企业年度、重算结果条数、草稿联动动作、
    各企业年度剩余未核验条数与告警（如已提交报告被退回草稿）。
    """
    activities = _select_pending(db, activity_ids, company_id, year)
    if not activities:
        return {
            "verified_count": 0,
            "affected": [],
            "recalculated": [],
            "reports": [],
            "remaining_unverified": {},
            "warnings": ["没有符合条件的待核验活动数据"],
        }

    pairs = sorted({(a.company_id, a.year) for a in activities})
    names = _company_names(db, {cid for cid, _ in pairs})
    _guard_approved_years(db, set(pairs), names)

    warnings: list[str] = []
    reports: list[dict] = []
    try:
        # 单一事务：核验标记、核算结果、报告草稿同生共死
        for act in activities:
            act.verified = 1
        db.flush()

        if recalculate:
            recalculated = []
            for cid, yr in pairs:
                count = recalc_company_year(db, cid, yr, commit=False)
                action, warning = _refresh_report_inner(db, cid, yr)
                # 写锁内复核：报告恰在批量执行期间被批准（配额已冻结）时，
                # 整批回滚，绝不留下“已核验/已重算但冻结快照仍旧”的年度。
                if action == "approved_blocked":
                    raise BatchVerifyError(
                        f"{names.get(cid, f'企业{cid}')}{yr}年度报告已批准并冻结配额，"
                        "不能再核验活动数据；请先冲正批准报告后重试"
                    )
                recalculated.append({"company_id": cid, "year": yr, "result_count": count})
                reports.append({"company_id": cid, "year": yr, "action": action})
                if warning:
                    warnings.append(warning)
        else:
            recalculated = []
            # 不重算时明确提示：草稿与核算结果均未更新，批准链路将被快照拦截
            warnings.append("本次仅核验未重算，须重新核算并刷新报告后方可批准")

        db.commit()
    except Exception:
        db.rollback()
        raise

    remaining = {}
    for cid, yr in pairs:
        remaining[f"{cid}:{yr}"] = (
            db.query(ActivityData)
            .filter(
                ActivityData.company_id == cid,
                ActivityData.year == yr,
                ActivityData.verified == 0,
            )
            .count()
        )

    return {
        "verified_count": len(activities),
        "affected": [
            {"company_id": cid, "company_name": names.get(cid, f"企业{cid}"), "year": yr}
            for cid, yr in pairs
        ],
        "recalculated": recalculated,
        "reports": reports,
        "remaining_unverified": remaining,
        "warnings": warnings,
    }
