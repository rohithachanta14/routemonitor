"""Phase 2 live-stack verification against real Docker services.

Run inside Docker:
    docker compose exec api python tests/phase2_verify.py
"""
from __future__ import annotations

import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx
from sqlalchemy import inspect, text

PASS = FAIL = SKIP = 0
RESULTS: list[tuple[str, str, str]] = []


def check(name: str, fn) -> None:
    global PASS, FAIL
    try:
        fn()
        PASS += 1
        RESULTS.append(("PASS", name, ""))
        print(f"  PASS  {name}")
    except Exception as e:
        FAIL += 1
        RESULTS.append(("FAIL", name, str(e)))
        print(f"  FAIL  {name}: {e}")


def skip(name: str, reason: str) -> None:
    global SKIP
    SKIP += 1
    RESULTS.append(("SKIP", name, reason))
    print(f"  SKIP  {name}: {reason}")


def test_bmp_parser() -> None:
    print("\n=== 1. BMP Parser ===")
    from core.bmp_parser import BMPParser
    from tests.fixtures.bgp_telemetry_generator import MockBGPTelemetryGenerator

    gen = MockBGPTelemetryGenerator()
    parser = BMPParser()

    def update_roundtrip():
        bmp = gen.generate_update("10.0.0.0/24", 65001, as_path=[65001, 65002, 65003])
        r = parser.parse_message(bmp)
        assert "10.0.0.0/24" in r["body"].nlri_prefixes
        assert r["body"].path_attributes["as_path"] == [65001, 65002, 65003]

    def withdraw_roundtrip():
        bmp = gen.generate_withdraw("192.168.0.0/16", 65001)
        r = parser.parse_message(bmp)
        assert "192.168.0.0/16" in r["body"].withdrawn_prefixes

    check("UPDATE roundtrip", update_roundtrip)
    check("WITHDRAW roundtrip", withdraw_roundtrip)


def test_influxdb_live() -> None:
    print("\n=== 2. InfluxDB (live) ===")
    from core.influxdb_connector import InfluxDBConnector

    def write_and_query():
        c = InfluxDBConnector()
        sid = f"phase2-{uuid.uuid4().hex[:8]}"
        c.write_metric(
            {
                "measurement": "route_stats",
                "tags": {"speaker_id": sid, "prefix": "10.0.0.0/24"},
                "fields": {"flap_count": 7, "route_count": 200, "path_diversity": 2.0},
            }
        )
        results = c.query_route_stats(sid, time_range="1h")
        c.close()
        assert isinstance(results, list)

    def batch_write():
        c = InfluxDBConnector()
        c.write_metrics_batch(
            [
                {
                    "measurement": "route_stats",
                    "tags": {"speaker_id": "batch-test"},
                    "fields": {"flap_count": 1},
                },
                {
                    "measurement": "route_stats",
                    "tags": {"speaker_id": "batch-test"},
                    "fields": {"flap_count": 2},
                },
            ]
        )
        c.close()

    def flap_baseline():
        c = InfluxDBConnector()
        baseline = c.query_flap_baseline("batch-test", days=1)
        c.close()
        assert "mean_flap_rate" in baseline

    check("write_metric + query_route_stats", write_and_query)
    check("write_metrics_batch", batch_write)
    check("query_flap_baseline", flap_baseline)


def test_celery_tasks_live() -> None:
    print("\n=== 3. Celery Tasks (live PostgreSQL + InfluxDB) ===")
    from api.database import SessionLocal
    from api.models import BGPSpeaker, RouteEvent
    from tasks.ingestion import compute_aggregates_task, parse_bmp_message_task
    from tests.fixtures.bgp_telemetry_generator import MockBGPTelemetryGenerator

    hostname = f"live-{uuid.uuid4().hex[:8]}"

    def full_pipeline():
        db = SessionLocal()
        speaker = BGPSpeaker(
            hostname=hostname,
            router_id="10.99.0.1",
            local_asn=65099,
            bmp_listen_address="10.99.0.1:179",
        )
        db.add(speaker)
        db.commit()
        db.refresh(speaker)
        speaker_id = speaker.id
        db.close()

        bmp = MockBGPTelemetryGenerator().generate_update("10.99.0.0/24", 65099)
        result = parse_bmp_message_task.run(bmp.hex())
        assert result["bgp_update"]["nlri_prefixes"] == ["10.99.0.0/24"]

        db2 = SessionLocal()
        events = db2.query(RouteEvent).filter(RouteEvent.speaker_id == speaker_id).all()
        assert len(events) >= 1
        assert events[0].event_type == "UPDATE"
        db2.query(RouteEvent).filter(RouteEvent.speaker_id == speaker_id).delete()
        db2.query(BGPSpeaker).filter(BGPSpeaker.id == speaker_id).delete()
        db2.commit()
        db2.close()

    def aggregates():
        result = compute_aggregates_task()
        assert "speakers_processed" in result

    check("parse → ingest pipeline (PostgreSQL)", full_pipeline)
    check("compute_aggregates_task", aggregates)


def test_api_live() -> None:
    print("\n=== 4. FastAPI Endpoints (live) ===")
    base = "http://localhost:8000"
    hostname = f"api-{uuid.uuid4().hex[:8]}"

    def health():
        r = httpx.get(f"{base}/health", timeout=5)
        assert r.status_code == 200
        assert r.json()["status"] == "healthy"

    def register_and_list():
        payload = {
            "hostname": hostname,
            "router_id": "10.50.0.1",
            "local_asn": 65050,
            "bmp_listen_address": "10.50.0.1:179",
        }
        r = httpx.post(f"{base}/api/telemetry/speakers", json=payload, timeout=5)
        assert r.status_code == 201
        speaker_id = r.json()["id"]

        r2 = httpx.get(f"{base}/api/telemetry/speakers", timeout=5)
        assert any(s["hostname"] == hostname for s in r2.json())

        r3 = httpx.get(f"{base}/api/telemetry/speakers/{speaker_id}/status", timeout=5)
        assert r3.status_code == 200
        assert "routes_advertised_24h" in r3.json()

    def bmp_ingest_async():
        from tests.fixtures.bgp_telemetry_generator import MockBGPTelemetryGenerator

        payload = {
            "hostname": f"{hostname}-bmp",
            "router_id": "10.51.0.1",
            "local_asn": 65051,
            "bmp_listen_address": "10.51.0.1:179",
        }
        httpx.post(f"{base}/api/telemetry/speakers", json=payload, timeout=5)
        bmp = MockBGPTelemetryGenerator().generate_update("10.51.0.0/24", 65051)
        r = httpx.post(f"{base}/api/telemetry/bmp/ingest", content=bmp, timeout=10)
        assert r.status_code == 202
        assert r.json()["status"] == "accepted"

    check("GET /health", health)
    check("speaker register + list + status", register_and_list)
    check("POST /bmp/ingest (async via Celery)", bmp_ingest_async)


def test_postgres() -> None:
    print("\n=== 5. PostgreSQL ===")
    from api.database import engine

    def tables():
        insp = inspect(engine)
        required = {"bgp_speakers", "route_events", "anomalies", "alerts"}
        assert required.issubset(set(insp.get_table_names()))

    def select_one():
        with engine.connect() as conn:
            assert conn.execute(text("SELECT 1")).scalar() == 1

    check("4 tables exist", tables)
    check("SELECT 1", select_one)


def main() -> int:
    print("=" * 60)
    print("RouteMonitor Phase 2 Live Verification")
    print("=" * 60)

    for fn in [
        test_bmp_parser,
        test_influxdb_live,
        test_celery_tasks_live,
        test_api_live,
        test_postgres,
    ]:
        try:
            fn()
        except Exception as e:
            print(f"  SECTION ERROR: {e}")

    print("\n" + "=" * 60)
    print(f"RESULTS: {PASS} passed, {FAIL} failed, {SKIP} skipped")
    print("=" * 60)
    if FAIL:
        for status, name, detail in RESULTS:
            if status == "FAIL":
                print(f"  - {name}: {detail}")
        return 1
    print("\nPhase 2 live stack: ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
