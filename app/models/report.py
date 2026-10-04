from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, Integer, Numeric, String, Text

from app.core.database import Base


class MrvReport(Base):
    """年度 MRV 报告：监测、报告与核查成果。"""

    __tablename__ = "mrv_reports"

    id = Column(Integer, primary_key=True)
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=False, index=True)
    year = Column(Integer, nullable=False, index=True)
    total_emission = Column(Numeric(18, 4), nullable=False, default=0)
    scope1 = Column(Numeric(18, 4), nullable=False, default=0)
    scope2 = Column(Numeric(18, 4), nullable=False, default=0)
    scope3 = Column(Numeric(18, 4), nullable=False, default=0)
    report_json = Column(Text, nullable=False, default="{}")   # 明细数据
    status = Column(String(16), nullable=False, default="draft")  # draft/submitted/approved/reversed
    generated_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    approved_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    approved_at = Column(DateTime, nullable=True)
    reversed_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    reversed_at = Column(DateTime, nullable=True)
    reversal_reason = Column(Text, nullable=False, default="")
