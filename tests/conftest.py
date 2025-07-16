"""pytest fixtures for RouteMonitor tests."""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from typing import Generator

# Disable BMP TCP server during tests (must be set before app import)
os.environ.setdefault("TESTING", "1")

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, types
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool
from sqlalchemy.types import TypeDecorator

from api.main import app
from api.models import Alert, Anomaly, Base, BGPSpeaker, RouteEvent, WebhookSubscription


class SQLiteUUID(TypeDecorator):
    """Store UUIDs as strings in SQLite, native UUID in PostgreSQL."""

    impl = types.String(36)
    cache_ok = True

    def load_dialect_impl(self, dialect):
        if dialect.name == "postgresql":
            return dialect.type_descriptor(PGUUID(as_uuid=True))
        return dialect.type_descriptor(types.String(36))

    def process_bind_param(self, value, dialect):
        if value is None:
            return value
        if dialect.name == "postgresql":
            return value
        return str(value)

    def process_result_value(self, value, dialect):
        if value is None:
            return value
        if isinstance(value, uuid.UUID):
            return value
        return uuid.UUID(str(value))


def _patch_sqlite_uuid() -> None:
    """Allow PostgreSQL UUID columns to work with SQLite in-memory tests."""
    for table in Base.metadata.tables.values():
        for column in table.columns:
            if isinstance(column.type, PGUUID):
                column.type = SQLiteUUID()


# ─── Celery eager mode for integration tests ─────────────────────────────────


@pytest.fixture(autouse=True)
def celery_eager_mode():
    """Run Celery tasks synchronously during tests."""
    from tasks.celery_app import app as celery_app

    celery_app.conf.task_always_eager = True
    celery_app.conf.task_eager_propagates = True
    yield
    celery_app.conf.task_always_eager = False
    celery_app.conf.task_eager_propagates = False


@pytest.fixture(autouse=True)
def patch_session_local(test_engine):
    """Route Celery tasks and BMP server to the same test database."""
    from unittest.mock import patch

    TestingSession = sessionmaker(autocommit=False, autoflush=False, bind=test_engine)
    with (
        patch("api.database.SessionLocal", TestingSession),
        patch("tasks.ingestion.SessionLocal", TestingSession),
        patch("api.bmp_server.SessionLocal", TestingSession),
    ):
        yield


# ─── Database ─────────────────────────────────────────────────────────────────


@pytest.fixture(scope="session")
def test_engine():
    """SQLite in-memory engine for the test session."""
    _patch_sqlite_uuid()
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def db_session(test_engine) -> Generator[Session, None, None]:
    """Per-test database session shared with Celery tasks via StaticPool."""
    SessionFactory = sessionmaker(bind=test_engine, autocommit=False, autoflush=False)
    session = SessionFactory()
    yield session
    session.rollback()
    for table in reversed(Base.metadata.sorted_tables):
        session.execute(table.delete())
    session.commit()
    session.close()


# ─── FastAPI test client ───────────────────────────────────────────────────────


@pytest.fixture
def client(db_session: Session) -> TestClient:
    """FastAPI TestClient with DB and auth dependencies overridden."""
    from api.auth import get_current_user as auth_get_current_user
    from api.dependencies import get_db

    def _get_db():
        yield db_session
        db_session.expire_all()

    async def _test_user():
        return {"username": "anonymous", "role": "admin"}

    app.dependency_overrides[get_db] = _get_db
    app.dependency_overrides[auth_get_current_user] = _test_user
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
def auth_client(db_session: Session) -> TestClient:
    """TestClient with real JWT auth (no get_current_user override)."""
    from api.dependencies import get_db

    def _get_db():
        yield db_session
        db_session.expire_all()

    app.dependency_overrides[get_db] = _get_db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


# ─── Model fixtures ───────────────────────────────────────────────────────────


@pytest.fixture
def mock_speaker(db_session: Session) -> BGPSpeaker:
    """Create a test BGP speaker."""
    speaker = BGPSpeaker(
        id=uuid.uuid4(),
        hostname="test-router-1",
        router_id="192.168.1.1",
        local_asn=65001,
        bmp_listen_address="192.168.1.1:179",
        status="CONNECTED",
        last_seen=datetime.now(timezone.utc),
    )
    db_session.add(speaker)
    db_session.commit()
    db_session.refresh(speaker)
    return speaker


@pytest.fixture
def mock_route_update(db_session: Session, mock_speaker: BGPSpeaker) -> RouteEvent:
    """Create a test BGP UPDATE route event."""
    event = RouteEvent(
        id=uuid.uuid4(),
        speaker_id=mock_speaker.id,
        timestamp=datetime.now(timezone.utc),
        event_type="UPDATE",
        prefix="10.0.0.0/24",
        path_attributes={
            "as_path": [65001, 65002, 65003],
            "next_hop": "192.168.1.2",
            "origin": "IGP",
            "local_pref": 100,
            "med": 0,
        },
        neighbor_ip="192.168.1.2",
        neighbor_asn=65002,
        sequence_number=1,
    )
    db_session.add(event)
    db_session.commit()
    db_session.refresh(event)
    return event


@pytest.fixture
def mock_withdrawal(db_session: Session, mock_speaker: BGPSpeaker) -> RouteEvent:
    """Create a test BGP WITHDRAW route event."""
    event = RouteEvent(
        id=uuid.uuid4(),
        speaker_id=mock_speaker.id,
        timestamp=datetime.now(timezone.utc),
        event_type="WITHDRAW",
        prefix="10.0.0.0/24",
        withdrawn_prefixes=["10.0.0.0/24"],
        neighbor_ip="192.168.1.2",
        neighbor_asn=65002,
        sequence_number=2,
    )
    db_session.add(event)
    db_session.commit()
    db_session.refresh(event)
    return event


@pytest.fixture
def mock_anomaly(db_session: Session, mock_speaker: BGPSpeaker) -> Anomaly:
    """Create a test anomaly."""
    anomaly = Anomaly(
        id=uuid.uuid4(),
        speaker_id=mock_speaker.id,
        prefix="10.0.0.0/24",
        anomaly_type="ROUTE_FLAP",
        severity="WARNING",
        detected_at=datetime.now(timezone.utc),
        details={
            "z_score": 4.2,
            "baseline_flap_rate": 1.0,
            "current_flap_rate": 12.0,
            "model": "z_score",
        },
        acknowledged=False,
    )
    db_session.add(anomaly)
    db_session.commit()
    db_session.refresh(anomaly)
    return anomaly


# ─── BGP telemetry generator fixture ─────────────────────────────────────────


@pytest.fixture
def mock_bgp_telemetry_generator():
    """Return an instance of the mock telemetry generator."""
    from tests.fixtures.bgp_telemetry_generator import MockBGPTelemetryGenerator

    return MockBGPTelemetryGenerator(num_speakers=3, prefixes_per_speaker=100)
