from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, Text

from app.core.database import Base


class Company(Base):
    """控排企业。"""

    __tablename__ = "companies"

    id = Column(Integer, primary_key=True)
    code = Column(String(64), unique=True, nullable=False, index=True)
    name = Column(String(128), nullable=False)
    industry = Column(String(64), nullable=False, default="")       # 行业（电力/钢铁/水泥/化工...）
    region = Column(String(64), nullable=False, default="")         # 所在地区
    boundary_desc = Column(Text, nullable=False, default="")        # 核算边界说明
    status = Column(String(16), nullable=False, default="active")   # active / inactive
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class EmissionScope(Base):
    """核算边界：范围一/二/三及其类别。"""

    __tablename__ = "emission_scopes"

    id = Column(Integer, primary_key=True)
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=False, index=True)
    scope = Column(String(2), nullable=False)       # 1 / 2 / 3
    category = Column(String(32), nullable=False, default="")
    name = Column(String(128), nullable=False, default="")
    description = Column(Text, nullable=False, default="")
