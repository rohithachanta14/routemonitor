"""Phase 4 live-stack verification for dashboard API dependencies.

Run inside Docker:
    docker compose exec api python tests/phase4_verify.py
"""
from __future__ import annotations

import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx

PASS = FAIL = 0
RESULTS: list[tuple[str, str, str]] = []

API = "http://localhost:8000"


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


def test_api_client() -> None:
    print("\n=== 1. RouteMonitorClient (all methods) ===")
    from dashboard.utils.api_client import RouteMonitorClient

    def full_client_smoke():
        with RouteMonitorClient(base_url=API) as c:
            health = c.health_check()
            assert health["status"] == "healthy"

            speakers = c.list_speakers()
            assert isinstance(speakers, list)

            anomalies = c.list_anomalies(time_range="24h")
            assert isinstance(anomalies, list)

            corr = c.get_correlation(time_range="7d")
            assert "matrix" in corr

            events = c.get_route_events(limit=10)
            assert isinstance(events, list)

            if speakers:
                sid = speakers[0]["id"]
                c.get_speaker(sid)
                c.get_speaker_status(sid)
                c.get_speaker_metrics(sid, "24h")
                c.get_route_stats(sid, time_range="24h")

    check("full client smoke", full_client_smoke)


def test_metrics_endpoints() -> None:
    print("\n=== 2. Metrics API ===")

    def correlation():
        r = httpx.get(
            f"{API}/api/metrics/correlation", params={"time_range": "7d"}, timeout=5
        )
        assert r.status_code == 200
        assert "matrix" in r.json()

    def correlation_validation():
        r = httpx.get(
            f"{API}/api/metrics/correlation", params={"top_n_prefixes": 1}, timeout=5
        )
        assert r.status_code == 422

    def speaker_metrics():
        r = httpx.get(f"{API}/api/telemetry/speakers", timeout=5)
        speakers = r.json()
        sid = speakers[0]["id"] if speakers else str(uuid.uuid4())
        r2 = httpx.get(
            f"{API}/api/metrics/speaker/{sid}", params={"time_range": "24h"}, timeout=5
        )
        assert r2.status_code == 200
        data = r2.json()
        for key in ("total_prefixes", "total_flaps", "anomaly_count", "uptime_pct"):
            assert key in data

    def invalid_uuid():
        r = httpx.get(f"{API}/api/metrics/speaker/bad-id", timeout=5)
        assert r.status_code == 422

    check("GET /api/metrics/correlation", correlation)
    check("correlation top_n validation", correlation_validation)
    check("GET /api/metrics/speaker/{id}", speaker_metrics)
    check("invalid speaker UUID → 422", invalid_uuid)


def test_telemetry_for_dashboard() -> None:
    print("\n=== 3. Telemetry endpoints (dashboard) ===")

    def route_events():
        r = httpx.get(
            f"{API}/api/telemetry/route-events", params={"limit": 5}, timeout=5
        )
        assert r.status_code == 200
        assert isinstance(r.json(), list)

    def route_stats():
        r = httpx.get(f"{API}/api/telemetry/speakers", timeout=5)
        speakers = r.json()
        if not speakers:
            return
        sid = speakers[0]["id"]
        r2 = httpx.get(
            f"{API}/api/telemetry/metrics/route-stats/{sid}",
            params={"time_range": "24h"},
            timeout=5,
        )
        assert r2.status_code == 200
        assert "data_points" in r2.json()

    check("GET /api/telemetry/route-events", route_events)
    check("GET route-stats", route_stats)


def test_dashboard_modules() -> None:
    print("\n=== 4. Dashboard modules ===")

    def pages():
        from dashboard.views import (
            anomaly_timeline,
            correlation_matrix,
            device_health,
            route_timeline,
        )

        assert callable(device_health.render)
        assert callable(route_timeline.render)
        assert callable(anomaly_timeline.render)
        assert callable(correlation_matrix.render)

    def formatting():
        from dashboard.utils.formatting import (
            anomaly_type_label,
            format_datetime,
            format_duration_seconds,
        )

        assert anomaly_type_label("ROUTE_FLAP") == "Route Flap"
        assert "UTC" in format_datetime("2024-01-01T00:00:00Z")
        assert format_duration_seconds(90) == "1.5m"

    check("dashboard pages import", pages)
    check("formatting helpers", formatting)


def main() -> int:
    print("=" * 60)
    print("RouteMonitor Phase 4 Live Verification")
    print("=" * 60)

    for fn in [
        test_api_client,
        test_metrics_endpoints,
        test_telemetry_for_dashboard,
        test_dashboard_modules,
    ]:
        try:
            fn()
        except Exception as e:
            print(f"  SECTION ERROR: {e}")

    print("\n" + "=" * 60)
    print(f"RESULTS: {PASS} passed, {FAIL} failed")
    print("=" * 60)
    if FAIL:
        for status, name, detail in RESULTS:
            if status == "FAIL":
                print(f"  - {name}: {detail}")
        return 1
    print("\nPhase 4 live stack: ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
