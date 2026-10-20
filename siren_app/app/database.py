from datetime import datetime, timezone

from sqlalchemy import DateTime, create_engine, event, inspect, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.types import TypeDecorator

from app.config import settings

# Bumped whenever the table layout changes. create_all() never alters an
# existing table, so an old file has to be noticed rather than half-used.
SCHEMA_VERSION = 3   # 3: triage_results.raw_colour


class Base(DeclarativeBase):
    pass


class UTCDateTime(TypeDecorator):
    """Stores UTC, always hands back an aware datetime.

    SQLite drops the offset, which made the browser read stored UTC times as
    local time and shift every timestamp by seven hours.
    """

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).replace(tzinfo=None)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        return value.replace(tzinfo=timezone.utc)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


_is_sqlite = settings.DATABASE_URL.startswith("sqlite")
engine = create_engine(
    settings.DATABASE_URL,
    connect_args={"check_same_thread": False} if _is_sqlite else {},
    future=True,
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False, future=True)


if _is_sqlite:
    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_connection, _record):
        # WAL survives a power cut mid-write far better than the default
        # rollback journal, and lets the monitor thread write while pages read.
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


class SchemaMismatchError(RuntimeError):
    pass


def create_db_and_tables() -> None:
    import app.models  # noqa: F401  registers the tables on Base

    existing = inspect(engine).get_table_names()
    with engine.begin() as conn:
        version = conn.execute(text("PRAGMA user_version")).scalar() if _is_sqlite else SCHEMA_VERSION
        if existing and version != SCHEMA_VERSION:
            raise SchemaMismatchError(
                f"Database {settings.DATABASE_URL} memakai skema lama (versi {version}, "
                f"dibutuhkan {SCHEMA_VERSION}). Pindahkan file tersebut sebagai cadangan, "
                "lalu jalankan ulang aplikasi untuk membuat database baru."
            )
    Base.metadata.create_all(bind=engine)
    if _is_sqlite:
        with engine.begin() as conn:
            conn.execute(text(f"PRAGMA user_version = {SCHEMA_VERSION}"))


def new_session() -> Session:
    return SessionLocal()
