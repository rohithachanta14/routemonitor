"""Phase 1 comprehensive verification script.

Run inside Docker:
    docker compose exec api python tests/phase1_verify.py
"""
from __future__ import annotations

import struct
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

# Ensure project root is on sys.path when run as a script
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx
from sqlalchemy import inspect, text

PASS = 0
FAIL = 0
SKIP = 0
RESULTS: list[tuple[str, str, str]] = []


def check(name: str, fn) -> None:
    global PASS, FAIL, SKIP
    try:
        fn()
        PASS += 1
        RESULTS.append(("PASS", name, ""))
        print(f"  PASS  {name}")
    except AssertionError as e:
        FAIL += 1
        RESULTS.append(("FAIL", name, str(e)))
        print(f"  FAIL  {name}: {e}")
    except Exception as e:
        FAIL += 1
        RESULTS.append(("FAIL", name, f"{type(e).__name__}: {e}"))
        print(f"  FAIL  {name}: {type(e).__name__}: {e}")


def skip(name: str, reason: str) -> None:
    global SKIP
    SKIP += 1
    RESULTS.append(("SKIP", name, reason))
    print(f"  SKIP  {name}: {reason}")


# ─── 1. Imports ───────────────────────────────────────────────────────────────


def test_imports() -> None:
    print("\n=== 1. Module Imports ===")

    def models():
        from api.models import Alert, Anomaly, BGPSpeaker, RouteEvent  # noqa: F401

    def schemas():
        from api.schemas import (
            AlertWebhookRequest,  # noqa: F401
            BGPSpeakerRequest,
            HealthResponse,
            RouteEventQueryParams,
        )

    def core():
        from core.bmp_parser import BMPParser  # noqa: F401
        from core.influxdb_connector import InfluxDBConnector  # noqa: F401

    def tasks():
        from tasks.celery_app import app  # noqa: F401
        from tasks.ingestion import parse_bmp_message_task  # noqa: F401

    def generator():
        from tests.fixtures.bgp_telemetry_generator import (
            MockBGPTelemetryGenerator,
        )  # noqa: F401

    check("import api.models", models)
    check("import api.schemas", schemas)
    check("import core modules", core)
    check("import celery tasks", tasks)
    check("import BGP telemetry generator", generator)


# ─── 2. Pydantic Schemas ──────────────────────────────────────────────────────


def test_schemas() -> None:
    print("\n=== 2. Pydantic Schema Validation ===")
    from pydantic import ValidationError

    from api.schemas import (
        AlertWebhookRequest,
        BGPSpeakerRequest,
        RouteEventQueryParams,
    )

    def valid_speaker():
        s = BGPSpeakerRequest(
            hostname="r1",
            router_id="10.0.0.1",
            local_asn=65000,
            bmp_listen_address="192.168.1.1:179",
        )
        assert s.local_asn == 65000

    def invalid_router_id():
        try:
            BGPSpeakerRequest(
                hostname="r1",
                router_id="bad",
                local_asn=65000,
                bmp_listen_address="192.168.1.1:179",
            )
            raise AssertionError("expected ValidationError")
        except ValidationError:
            pass

    def invalid_asn():
        try:
            BGPSpeakerRequest(
                hostname="r1",
                router_id="10.0.0.1",
                local_asn=0,
                bmp_listen_address="192.168.1.1:179",
            )
            raise AssertionError("expected ValidationError")
        except ValidationError:
            pass

    def valid_cidr():
        p = RouteEventQueryParams(prefix="10.0.0.0/24")
        assert p.prefix == "10.0.0.0/24"

    def invalid_event_type():
        try:
            RouteEventQueryParams(event_type="BOGUS")
            raise AssertionError("expected ValidationError")
        except ValidationError:
            pass

    def valid_webhook():
        w = AlertWebhookRequest(target_url="https://hooks.example.com/alert")
        assert w.severity_min == "WARNING"

    def invalid_webhook_url():
        try:
            AlertWebhookRequest(target_url="ftp://bad")
            raise AssertionError("expected ValidationError")
        except ValidationError:
            pass

    check("BGPSpeakerRequest valid", valid_speaker)
    check("BGPSpeakerRequest rejects bad router_id", invalid_router_id)
    check("BGPSpeakerRequest rejects ASN=0", invalid_asn)
    check("RouteEventQueryParams accepts CIDR", valid_cidr)
    check("RouteEventQueryParams rejects bad event_type", invalid_event_type)
    check("AlertWebhookRequest valid", valid_webhook)
    check("AlertWebhookRequest rejects non-http URL", invalid_webhook_url)


# ─── 3. PostgreSQL + Alembic ─────────────────────────────────────────────────


def test_database() -> None:
    print("\n=== 3. PostgreSQL + Alembic ===")
    from api.database import SessionLocal, engine
    from api.models import BGPSpeaker

    def select_one():
        with engine.connect() as conn:
            assert conn.execute(text("SELECT 1")).scalar() == 1

    def four_tables_exist():
        insp = inspect(engine)
        tables = set(insp.get_table_names())
        required = {"bgp_speakers", "route_events", "anomalies", "alerts"}
        missing = required - tables
        assert not missing, f"missing tables: {missing}"

    def alembic_version_table():
        insp = inspect(engine)
        assert "alembic_version" in insp.get_table_names()

    def orm_roundtrip():
        session = SessionLocal()
        try:
            speaker = BGPSpeaker(
                id=uuid4(),
                hostname=f"verify-{uuid4().hex[:8]}",
                router_id="10.99.0.1",
                local_asn=65099,
                bmp_listen_address="10.99.0.1:179",
                status="CONNECTED",
                last_seen=datetime.now(timezone.utc),
            )
            session.add(speaker)
            session.commit()
            found = (
                session.query(BGPSpeaker).filter_by(hostname=speaker.hostname).first()
            )
            assert found is not None
            assert found.local_asn == 65099
            session.delete(found)
            session.commit()
        finally:
            session.close()

    check("PostgreSQL SELECT 1", select_one)
    check("4 ORM tables exist", four_tables_exist)
    check("alembic_version table exists", alembic_version_table)
    check("ORM insert/query/delete roundtrip", orm_roundtrip)


# ─── 4. InfluxDB Connector ───────────────────────────────────────────────────


def test_influxdb() -> None:
    print("\n=== 4. InfluxDB Connector ===")
    import os

    from core.influxdb_connector import InfluxDBConnector

    def health_endpoint():
        url = os.getenv("INFLUXDB_URL", "http://influxdb:8086")
        r = httpx.get(f"{url}/health", timeout=5.0)
        assert r.status_code == 200

    def write_and_close():
        connector = InfluxDBConnector()
        connector.write_metric(
            {
                "measurement": "route_stats",
                "tags": {
                    "speaker_id": "phase1-verify",
                    "prefix": "10.0.0.0/24",
                    "event_type": "UPDATE",
                },
                "fields": {
                    "flap_count": 3,
                    "route_count": 500,
                    "path_diversity": 2.5,
                },
            }
        )
        connector.close()

    def init_creates_clients():
        c = InfluxDBConnector()
        assert c.client is not None
        assert c.write_api is not None
        assert c.query_api is not None
        c.close()

    check("InfluxDB /health endpoint", health_endpoint)
    check("InfluxDBConnector __init__", init_creates_clients)
    check("InfluxDBConnector write_metric + close", write_and_close)


# ─── 5. Health API Endpoint ──────────────────────────────────────────────────


def test_health_api() -> None:
    print("\n=== 5. FastAPI Health Endpoint ===")
    from fastapi.testclient import TestClient

    from api.main import app

    client = TestClient(app)

    def health_returns_200():
        r = client.get("/health")
        assert r.status_code == 200

    def health_status_healthy():
        body = client.get("/health").json()
        assert body["status"] == "healthy"
        assert body["version"] == "0.1.0"

    def health_all_services_ok():
        body = client.get("/health").json()
        services = body["services"]
        assert services["db"] == "ok"
        assert services["redis"] == "ok"
        assert services["influxdb"] == "ok"

    def swagger_docs_loads():
        r = client.get("/docs")
        assert r.status_code == 200
        assert "swagger" in r.text.lower() or "openapi" in r.text.lower()

    def openapi_json():
        r = client.get("/openapi.json")
        assert r.status_code == 200
        spec = r.json()
        assert spec["info"]["title"] == "RouteMonitor"
        assert "/health" in spec["paths"]

    check("GET /health returns 200", health_returns_200)
    check("GET /health status=healthy", health_status_healthy)
    check("GET /health all services ok", health_all_services_ok)
    check("GET /docs loads Swagger UI", swagger_docs_loads)
    check("GET /openapi.json valid", openapi_json)


# ─── 6. Redis / Celery ───────────────────────────────────────────────────────


def test_redis_celery() -> None:
    print("\n=== 6. Redis + Celery ===")
    from tasks.celery_app import app as celery_app

    def celery_ping():
        result = celery_app.control.ping(timeout=5.0)
        assert result, "no celery workers responded to ping"

    def redis_broker_reachable():
        # ping implies broker (Redis) is reachable
        result = celery_app.control.ping(timeout=5.0)
        assert len(result) >= 1

    check("Celery control.ping()", celery_ping)
    check("Redis broker reachable via Celery", redis_broker_reachable)


# ─── 7. BGP Telemetry Generator ──────────────────────────────────────────────


def test_bgp_generator() -> None:
    print("\n=== 7. BGP Telemetry Generator (Binary Encoding) ===")
    from tests.fixtures.bgp_telemetry_generator import (
        BMP_MSG_ROUTE_MONITORING,
        BMP_VERSION,
        MockBGPTelemetryGenerator,
    )

    gen = MockBGPTelemetryGenerator(num_speakers=2, prefixes_per_speaker=10)

    def update_is_bytes():
        msg = gen.generate_update("10.0.0.0/24", 65001, as_path=[65001, 65002])
        assert isinstance(msg, bytes)
        assert len(msg) > 42

    def update_bmp_header():
        msg = gen.generate_update("192.168.1.0/24", 65001)
        assert msg[0] == BMP_VERSION
        msg_len = struct.unpack(">I", msg[1:5])[0]
        assert msg_len == len(msg)
        assert msg[5] == BMP_MSG_ROUTE_MONITORING

    def withdraw_is_bytes():
        msg = gen.generate_withdraw("10.0.0.0/24")
        assert isinstance(msg, bytes)
        assert len(msg) > 42
        assert msg[0] == BMP_VERSION
        assert msg[5] == BMP_MSG_ROUTE_MONITORING

    def per_peer_header_42_bytes():
        hdr = gen._build_per_peer_header(65100, "10.1.0.1", "10.0.0.1")
        assert len(hdr) == 42

    def flap_simulation():
        msgs = gen.simulate_route_flap("router-1", "10.0.0.0/24", num_flaps=5)
        assert len(msgs) == 5
        for m in msgs:
            assert isinstance(m, bytes) and len(m) > 42

    def link_failure_simulation():
        msgs = gen.simulate_link_failure(affected_prefix_count=10)
        assert len(msgs) == 10

    def normal_traffic():
        msgs = gen.simulate_normal_traffic(num_messages=50)
        assert len(msgs) == 50

    check("generate_update returns valid BMP bytes", update_is_bytes)
    check("BMP common header fields correct (UPDATE)", update_bmp_header)
    check("generate_withdraw returns valid BMP bytes", withdraw_is_bytes)
    check("per-peer header is exactly 42 bytes", per_peer_header_42_bytes)
    check("simulate_route_flap produces messages", flap_simulation)
    check("simulate_link_failure produces withdrawals", link_failure_simulation)
    check("simulate_normal_traffic produces messages", normal_traffic)


# ─── 8. Phase 2/3 stubs (informational) ─────────────────────────────────────


def report_future_phase_stubs() -> None:
    print("\n=== 8. Historical Phase Stubs (informational) ===")
    skip("Phase 2–5 stubs", "All phases implemented — see phase2–5 verify scripts")


def main() -> int:
    print("=" * 60)
    print("RouteMonitor Phase 1 Verification")
    print("=" * 60)

    for fn in [
        test_imports,
        test_schemas,
        test_database,
        test_influxdb,
        test_health_api,
        test_redis_celery,
        test_bgp_generator,
        report_future_phase_stubs,
    ]:
        try:
            fn()
        except Exception:
            traceback.print_exc()

    print("\n" + "=" * 60)
    print(f"RESULTS: {PASS} passed, {FAIL} failed, {SKIP} skipped")
    print("=" * 60)

    if FAIL:
        print("\nFailed checks:")
        for status, name, detail in RESULTS:
            if status == "FAIL":
                print(f"  - {name}: {detail}")
        return 1

    print("\nPhase 1 foundation: ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
