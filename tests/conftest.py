import os
import sys
import tempfile
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# 在导入任何 app 模块前，将数据库指向临时文件，避免污染正式库
_tmp_dir = tempfile.mkdtemp()
os.environ.setdefault("CARBON_DATABASE_URL", f"sqlite:///{_tmp_dir}/pytest.db")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.database import Base  # noqa: E402
from app.core.event_hooks import install_ledger_hooks  # noqa: E402


@pytest.fixture()
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    TestSession = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    install_ledger_hooks(TestSession)
    session = TestSession()
    yield session
    session.close()


@pytest.fixture()
def seed(db):
    """构造一家带边界/因子/方法的基础企业。"""
    from app.models import CalculationMethod, Company, EmissionFactor, EmissionScope

    company = Company(code="T-001", name="测试企业", industry="电力", region="测试区")
    db.add(company)
    db.flush()

    scope1 = EmissionScope(company_id=company.id, scope="1", category="燃料燃烧", name="直接排放")
    scope2 = EmissionScope(company_id=company.id, scope="2", category="外购电力", name="厂区用电")
    db.add_all([scope1, scope2])
    db.flush()

    method_elec = CalculationMethod(method_code="ELEC", name="外购电力排放因子法", scope="2", formula_type="activity_factor")
    method_fuel = CalculationMethod(
        method_code="FUEL", name="燃料燃烧缺省值法", scope="1", formula_type="fuel_combustion",
        params='{"carbon_oxidation": 0.98}',
    )
    db.add_all([method_elec, method_fuel])
    db.flush()

    factor_elec = EmissionFactor(
        factor_code="ELEC-GRID", name="外购电力", scope="2", unit="tCO2/MWh", value=0.5703,
        source="电网因子", valid_from="2024-01-01", valid_to="2025-12-31",
    )
    factor_coal = EmissionFactor(
        factor_code="COAL-PWR", name="燃煤消耗", scope="1", unit="tC/t", value=2.6,
        source="综合系数", valid_from="2024-01-01", valid_to=None,
    )
    db.add_all([factor_elec, factor_coal])
    db.commit()

    return {
        "company": company,
        "scope1": scope1,
        "scope2": scope2,
        "method_elec": method_elec,
        "method_fuel": method_fuel,
        "factor_elec": factor_elec,
        "factor_coal": factor_coal,
    }
