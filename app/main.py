from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.api import activity, auction, auth, calculation, companies, dashboard, factors, ledger, loans, quotas, reports, trade_orders

BASE_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = BASE_DIR / "static"

app = FastAPI(title="碳排放核算与交易管理系统", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

for router in (
    auth.router,
    companies.router,
    activity.router,
    factors.router,
    calculation.router,
    quotas.router,
    reports.router,
    trade_orders.router,
    loans.router,
    auction.router,
    ledger.router,
    dashboard.router,
):
    app.include_router(router)

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(str(STATIC_DIR / "index.html"))


@app.get("/{path:path}", include_in_schema=False)
def spa_fallback(path: str):
    full = STATIC_DIR / path
    if path and full.exists() and full.is_file():
        return FileResponse(str(full))
    return FileResponse(str(STATIC_DIR / "index.html"))
