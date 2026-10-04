"""核算引擎：按核算方法计算排放量并汇总年度结果。

- activity_factor：排放量 = 活动量 × 排放因子值
- fuel_combustion：排放量 = 燃料量 × 综合因子 × 碳氧化率 × 44/12
因子按生效日期区间取当期有效版本。
"""

import json
from datetime import date

from sqlalchemy.orm import Session

from app.models.emission import ActivityData, CalculationMethod, EmissionFactor, EmissionResult


def get_factor_for_year(db: Session, factor_name: str, year: int) -> EmissionFactor | None:
    """按活动类型名称取该年生效的因子（valid_from 最晚的优先）。"""
    start = date(year, 1, 1).isoformat()
    end = date(year, 12, 31).isoformat()
    factors = (
        db.query(EmissionFactor)
        .filter(
            EmissionFactor.name == factor_name,
            EmissionFactor.valid_from <= end,
        )
        .all()
    )
    candidates = [f for f in factors if (not f.valid_to or f.valid_to >= start)]
    if not candidates:
        return None
    return max(candidates, key=lambda f: f.valid_from)


def compute_emission(
    activity: ActivityData, factor: EmissionFactor, method: CalculationMethod | None
) -> float:
    """计算单条活动数据的排放量（tCO2e）。"""
    quantity = float(activity.quantity)
    factor_value = float(factor.value)
    formula = method.formula_type if method else "activity_factor"
    if formula == "fuel_combustion":
        params = json.loads(method.params or "{}")
        oxidation = float(params.get("carbon_oxidation", 1.0))
        return quantity * factor_value * oxidation * 44 / 12
    return quantity * factor_value


def count_unverified_activities(db: Session, company_id: int, year: int) -> int:
    """统计企业某年度尚未经核查员核验的活动数据条数。

    核算链路的数据状态约束：未核验（verified=0）的活动数据不得进入排放核算，
    也不得进一步影响 MRV 报告、配额冻结与履约结果。
    """
    return (
        db.query(ActivityData)
        .filter(
            ActivityData.company_id == company_id,
            ActivityData.year == year,
            ActivityData.verified == 0,
        )
        .count()
    )


def recalc_company_year(db: Session, company_id: int, year: int, commit: bool = True) -> int:
    """重算某企业某年度全部**已核验**活动数据的排放量（先清除旧结果保证幂等）。

    数据状态约束：仅核查员核验通过（verified=1）的活动数据才允许进入核算，
    未核验数据在源头被排除，避免污染核算结果及其后的年度报告、配额冻结和履约闭环。
    活动数据完成核验后需重新触发核算方可计入结果。

    ``commit=False`` 时不自行提交，供批量核验在更大的事务内组合调用，
    使“核验 → 重算 → 报告草稿联动”同生共死、任一步失败整体回滚。
    """
    db.query(EmissionResult).filter(
        EmissionResult.company_id == company_id, EmissionResult.year == year
    ).delete()
    db.flush()

    activities = (
        db.query(ActivityData)
        .filter(
            ActivityData.company_id == company_id,
            ActivityData.year == year,
            ActivityData.verified == 1,
        )
        .all()
    )
    count = 0
    for act in activities:
        factor = get_factor_for_year(db, act.activity_type, year)
        method = None
        if factor:
            method = (
                db.query(CalculationMethod)
                .filter(CalculationMethod.formula_type == "fuel_combustion", CalculationMethod.scope == factor.scope)
                .first()
            )
            emission = compute_emission(act, factor, method)
            db.add(
                EmissionResult(
                    company_id=company_id,
                    scope_id=act.scope_id,
                    year=year,
                    activity_id=act.id,
                    factor_id=factor.id,
                    method_code=method.method_code if method else "",
                    activity_quantity=act.quantity,
                    factor_value=factor.value,
                    emission_amount=round(emission, 6),
                )
            )
            count += 1
    db.flush()
    if commit:
        db.commit()
    return count


def scope_totals(db: Session, company_id: int, year: int) -> dict:
    """按范围汇总年度排放量。"""
    totals = {"1": 0.0, "2": 0.0, "3": 0.0}
    results = (
        db.query(EmissionResult)
        .filter(EmissionResult.company_id == company_id, EmissionResult.year == year)
        .all()
    )
    scope_map = {}
    for r in results:
        if r.scope_id not in scope_map:
            scope_map[r.scope_id] = None
    from app.models.company import EmissionScope

    scopes = db.query(EmissionScope).filter(EmissionScope.id.in_(scope_map.keys())).all()
    scope_map = {s.id: s.scope for s in scopes}
    for r in results:
        key = scope_map.get(r.scope_id, "1")
        totals[key] = totals.get(key, 0.0) + float(r.emission_amount)
    return {k: round(v, 4) for k, v in totals.items()}


def annual_total(db: Session, company_id: int, year: int) -> float:
    return round(sum(scope_totals(db, company_id, year).values()), 4)
