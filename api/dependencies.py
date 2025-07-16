"""FastAPI dependency functions (db session, auth, logging, services)."""
from typing import Generator

import structlog
from sqlalchemy.orm import Session

from api.database import SessionLocal
from core.config import settings
from core.influxdb_connector import InfluxDBConnector

logger = structlog.get_logger(__name__)


# ─── Database ─────────────────────────────────────────────────────────────────


def get_db() -> Generator[Session, None, None]:
    """Yield a SQLAlchemy session; always close on exit."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ─── InfluxDB ─────────────────────────────────────────────────────────────────


def get_influxdb_connector() -> InfluxDBConnector:
    """Return a singleton InfluxDB connector."""
    return InfluxDBConnector(
        url=settings.INFLUXDB_URL,
        token=settings.INFLUXDB_TOKEN,
        org=settings.INFLUXDB_ORG,
        bucket=settings.INFLUXDB_BUCKET,
    )


# ─── Auth ─────────────────────────────────────────────────────────────────────

from api.auth import get_current_user  # noqa: E402 — re-export for existing imports

# ─── Logger ───────────────────────────────────────────────────────────────────


def get_logger(name: str = "routemonitor") -> structlog.BoundLogger:
    return structlog.get_logger(name)
