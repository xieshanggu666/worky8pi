from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, Integer, Numeric, String, Text

from app.core.database import Base


class ActivityData(Base):
    """活动数据台账：企业各核算边界下的活动量记录。"""

    __tablename__ = "activity_data"

    id = Column(Integer, primary_key=True)
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=False, index=True)
    scope_id = Column(Integer, ForeignKey("emission_scopes.id"), nullable=True)
    year = Column(Integer, nullable=False, index=True)
    period = Column(String(16), nullable=False, default="monthly")   # monthly/quarterly/annual
    activity_type = Column(String(64), nullable=False)               # 燃煤消耗/外购电力/外购热力...
    unit = Column(String(32), nullable=False, default="")
    quantity = Column(Numeric(18, 4), nullable=False, default=0)
    data_source = Column(String(128), nullable=False, default="")
    recorded_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    record_date = Column(DateTime, nullable=False, default=datetime.utcnow)
    verified = Column(Integer, nullable=False, default=0)            # 核查员核验 0/1
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class EmissionFactor(Base):
    """排放因子库：燃料/电力/热力/工艺排放因子，含有效期限。"""

    __tablename__ = "emission_factors"

    id = Column(Integer, primary_key=True)
    factor_code = Column(String(64), unique=True, nullable=False, index=True)
    name = Column(String(128), nullable=False)
    scope = Column(String(2), nullable=False, default="1")
    unit = Column(String(32), nullable=False, default="tCO2/单位")   # 因子单位
    value = Column(Numeric(18, 6), nullable=False, default=0)        # 因子值
    source = Column(String(128), nullable=False, default="")         # 数据来源（方法学/标准）
    valid_from = Column(String(10), nullable=False, default="")      # YYYY-MM-DD
    valid_to = Column(String(10), nullable=True)                     # 空表示长期有效
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class FactorVersion(Base):
    """排放因子版本历史：因子值修订留痕。"""

    __tablename__ = "factor_versions"

    id = Column(Integer, primary_key=True)
    factor_id = Column(Integer, ForeignKey("emission_factors.id"), nullable=False, index=True)
    version_no = Column(Integer, nullable=False, default=1)
    value = Column(Numeric(18, 6), nullable=False, default=0)
    valid_from = Column(String(10), nullable=False, default="")
    note = Column(String(256), nullable=False, default="")
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class CalculationMethod(Base):
    """核算方法模板：定义计算公式与参数。"""

    __tablename__ = "calculation_methods"

    id = Column(Integer, primary_key=True)
    method_code = Column(String(64), unique=True, nullable=False, index=True)
    name = Column(String(128), nullable=False)
    scope = Column(String(2), nullable=False, default="1")
    formula_type = Column(String(32), nullable=False, default="activity_factor")
    # activity_factor: 排放量 = 活动量 × 因子值
    # fuel_combustion: 排放量 = 燃料量 × 低位发热量 × 单位热值含碳量 × 碳氧化率 × 44/12
    params = Column(Text, nullable=False, default="{}")             # 附加参数 JSON
    description = Column(Text, nullable=False, default="")


class EmissionResult(Base):
    """核算结果：每条活动数据对应的排放量。"""

    __tablename__ = "emission_results"

    id = Column(Integer, primary_key=True)
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=False, index=True)
    scope_id = Column(Integer, ForeignKey("emission_scopes.id"), nullable=True)
    year = Column(Integer, nullable=False, index=True)
    activity_id = Column(Integer, ForeignKey("activity_data.id"), nullable=False)
    factor_id = Column(Integer, ForeignKey("emission_factors.id"), nullable=True)
    method_code = Column(String(64), nullable=False, default="")
    activity_quantity = Column(Numeric(18, 4), nullable=False, default=0)
    factor_value = Column(Numeric(18, 6), nullable=False, default=0)
    emission_amount = Column(Numeric(18, 6), nullable=False, default=0)   # tCO2e
    calculated_at = Column(DateTime, nullable=False, default=datetime.utcnow)
