from sqlalchemy import create_engine, event
from sqlalchemy.orm import declarative_base, sessionmaker

from app.core.config import DATABASE_URL

_is_sqlite = DATABASE_URL.startswith("sqlite")

engine = create_engine(
    DATABASE_URL,
    # SQLite 写锁为库级锁：并发清缴/交易时让后到的事务等待，而非立刻报 database is locked
    connect_args={"check_same_thread": False, "timeout": 30} if _is_sqlite else {},
)

if _is_sqlite:

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragma(dbapi_connection, _connection_record):
        cursor = dbapi_connection.cursor()
        # busy_timeout 与 connect_args 的 timeout 双保险：写冲突时等待而非立即失败
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.close()


SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()

# 统一账本事件钩子：任何会话新写入的配额流水都自动同事务登记为账本事件
from app.core.event_hooks import install_ledger_hooks  # noqa: E402

install_ledger_hooks(SessionLocal)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
