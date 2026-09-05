"""Engine, session factory, declarative Base. Lazily bound so tests and the chaos
harness can point the agent at a throwaway database (or an unreachable one)."""
from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from . import config

_engine: Engine | None = None
_SessionLocal: sessionmaker | None = None
_url: str = config.DATABASE_URL


class Base(DeclarativeBase):
    pass


def configure(url: str) -> None:
    """Rebind the agent to a different database URL (tests, chaos harness)."""
    global _engine, _SessionLocal, _url
    if _engine is not None:
        _engine.dispose()
    _engine, _SessionLocal, _url = None, None, url


def engine() -> Engine:
    global _engine, _SessionLocal
    if _engine is None:
        if _url.startswith("sqlite"):
            _engine = create_engine(_url, connect_args={"check_same_thread": False})

            @event.listens_for(_engine, "connect")
            def _pragmas(dbapi_conn, _record):
                dbapi_conn.execute("PRAGMA foreign_keys=ON")
        else:
            _engine = create_engine(_url, pool_pre_ping=True)
        _SessionLocal = sessionmaker(bind=_engine, autoflush=False, autocommit=False, expire_on_commit=False)
    return _engine


def session() -> Session:
    engine()
    assert _SessionLocal is not None
    return _SessionLocal()


def init_db() -> None:
    """Create tables. This project has no migrations on purpose (4-hour scope; see ARCHITECTURE.md)."""
    from . import models  # noqa: F401  (registers tables on Base)
    Base.metadata.create_all(engine())


def url() -> str:
    return _url
