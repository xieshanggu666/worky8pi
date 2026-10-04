import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent.parent

DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

DATABASE_URL = os.getenv("CARBON_DATABASE_URL", f"sqlite:///{DATA_DIR / 'app.db'}")

SECRET_KEY = os.getenv("CARBON_SECRET_KEY", "carbon-system-dev-secret-change-me")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 8

SCOPES = ["1", "2", "3"]
PERIODS = ["monthly", "quarterly", "annual"]
QUOTA_STATUSES = ["pending", "allocated", "frozen", "cleared"]
COMPLIANCE_STATUSES = ["pending", "compliant", "deficit", "reversed"]
REPORT_STATUSES = ["draft", "submitted", "approved", "reversed"]
