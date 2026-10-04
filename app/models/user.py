from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, Integer, String

from app.core.database import Base


class User(Base):
    """平台用户：admin 监管管理员 / enterprise 控排企业 / verifier 核查员。"""

    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    username = Column(String(64), unique=True, nullable=False, index=True)
    display_name = Column(String(64), nullable=False, default="")
    email = Column(String(128), nullable=False, default="")
    company_id = Column(Integer, nullable=True)  # enterprise 用户关联的控排企业
    password_hash = Column(String(128), nullable=False)
    salt = Column(String(64), nullable=False)
    role = Column(String(16), nullable=False, default="enterprise")
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
