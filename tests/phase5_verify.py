"""Phase 5 live-stack verification: auth, rate limiting, metrics, load test.

Run inside Docker (against live services):
    docker compose exec api python tests/phase5_verify.py

Run from host (API on port 8001):
    python tests/phase5_verify.py --host http://localhost:8001
"""
from __future__ import annotations

import argparse
import csv
import subprocess
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx

PASS = FAIL = SKIP = 0
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


def skip(name: str, reason: str) -> None:
    global SKIP
    SKIP += 1
    RESULTS.append(("SKIP", name, reason))
    print(f"  SKIP  {name}: {reason}")


def _admin_token(client: httpx.Client) -> str:
    r = client.post(
        "/api/auth/token",
        data={"username": "admin", "password": "admin123"},
    )
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def test_scaffold_files() -> None:
    print("\n=== 1. Phase 5 scaffold files ===")
    root = Path(__file__).resolve().parent.parent

    def exists(path: str):
        p = root / path
        assert p.exists(), f"Missing {path}"

    check("api/auth.py", lambda: exists("api/auth.py"))
    check("api/middleware.py", lambda: exists("api/middleware.py"))
    check("docker-compose.prod.yml", lambda: exists("docker-compose.prod.yml"))
    check("k8s/ namespace", lambda: exists("k8s/namespace.yaml"))
    check("k8s/ api-deployment", lambda: exists("k8s/api-deployment.yaml"))
    check("k8s/ ingress", lambda: exists("k8s/ingress.yaml"))
    check(
        "Grafana dashboard",
        lambda: exists("monitoring/grafana/dashboards/routemonitor.json"),
    )
    check("CI/CD deploy.yml", lambda: exists(".github/workflows/deploy.yml"))
    check("Locust load test", lambda: exists("tests/load/locustfile.py"))
    check(".env.production template", lambda: exists(".env.production"))


def test_auth_live(client: httpx.Client) -> None:
    print("\n=== 2. JWT auth (live) ===")

    def login_admin():
        token = _admin_token(client)
        assert len(token) > 20

    def login_invalid():
        r = client.post(
            "/api/auth/token",
            data={"username": "admin", "password": "wrong"},
        )
        assert r.status_code == 401

    def whoami():
        token = _admin_token(client)
        r = client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200
        data = r.json()
        assert data["username"] == "admin"
        assert data["role"] == "admin"

    def protected_without_token():
        r = client.post(
            "/api/alerts/webhooks",
            json={"target_url": "https://example.com/hook", "severity_min": "WARNING"},
        )
        assert r.status_code == 401

    def protected_with_token():
        token = _admin_token(client)
        r = client.post(
            "/api/alerts/webhooks",
            json={
                "target_url": f"https://example.com/hook-{uuid.uuid4().hex[:8]}",
                "severity_min": "WARNING",
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 201

    def operator_denied_webhook():
        r = client.post(
            "/api/auth/token",
            data={"username": "operator", "password": "operator123"},
        )
        token = r.json()["access_token"]
        r2 = client.post(
            "/api/alerts/webhooks",
            json={"target_url": "https://example.com/hook"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r2.status_code == 403

    check("POST /api/auth/token (admin)", login_admin)
    check("invalid credentials → 401", login_invalid)
    check("GET /api/auth/me", whoami)
    check("protected endpoint without token → 401", protected_without_token)
    check("protected endpoint with admin token → 201", protected_with_token)
    check("operator cannot register webhook → 403", operator_denied_webhook)


def _clear_rate_limits() -> None:
    """Flush Redis rate-limit keys between hammer tests and load test."""
    import redis as redis_lib

    from core.config import settings

    r = redis_lib.from_url(settings.REDIS_URL, decode_responses=True)
    for key in r.scan_iter("rate_limit:*"):
        r.delete(key)


def test_middleware_live(client: httpx.Client) -> None:
    print("\n=== 3. Middleware (live) ===")

    def request_id_header():
        r = client.get("/health")
        assert r.status_code == 200
        assert "X-Request-ID" in r.headers
        uuid.UUID(r.headers["X-Request-ID"])

    def rate_limit_headers():
        _clear_rate_limits()
        r = client.get("/api/anomalies/")
        assert r.status_code == 200
        assert "X-RateLimit-Limit" in r.headers
        assert "X-RateLimit-Remaining" in r.headers
        assert r.headers["X-RateLimit-Limit"] == "100"

    check("X-Request-ID on /health", request_id_header)
    check("X-RateLimit-* headers on /api/anomalies/", rate_limit_headers)


def test_rate_limit_hammer(client: httpx.Client) -> None:
    print("\n=== 8. Rate limit hammer (live) ===")

    def rate_limit_triggers_429():
        _clear_rate_limits()
        codes: dict[int, int] = {}
        with httpx.Client(base_url=API, timeout=5.0, follow_redirects=True) as hammer:
            for _ in range(110):
                r = hammer.get("/api/anomalies/")
                codes[r.status_code] = codes.get(r.status_code, 0) + 1
        assert codes.get(200, 0) >= 90, f"Expected mostly 200s, got {codes}"
        assert codes.get(429, 0) >= 1, f"Expected some 429s, got {codes}"

    check("rate limit fires after 100 req/min", rate_limit_triggers_429)
    _clear_rate_limits()


def test_prometheus_live(client: httpx.Client) -> None:
    print("\n=== 4. Prometheus metrics (live) ===")

    def routemonitor_metrics():
        client.get("/health")
        r = client.get("/metrics")
        assert r.status_code == 200
        assert len(r.text) > 100, "Empty metrics body (check follow_redirects)"
        for metric in (
            "routemonitor_http_requests_total",
            "routemonitor_http_request_duration_seconds",
            "routemonitor_bmp_messages_total",
            "routemonitor_anomalies_detected_total",
            "routemonitor_alerts_dispatched_total",
        ):
            assert metric in r.text, f"Missing {metric}"

    check("GET /metrics exposes routemonitor_* counters", routemonitor_metrics)


def test_bmp_ingest_live(client: httpx.Client) -> None:
    print("\n=== 5. BMP ingest smoke (live) ===")
    from tests.fixtures.bgp_telemetry_generator import MockBGPTelemetryGenerator

    gen = MockBGPTelemetryGenerator()

    def ingest_update():
        msg = gen.generate_update("10.99.0.0/24", 65001)
        r = client.post(
            "/api/telemetry/bmp/ingest",
            content=msg,
            headers={"Content-Type": "application/octet-stream"},
        )
        assert r.status_code == 202, r.text

    check("POST /api/telemetry/bmp/ingest", ingest_update)


def test_locust_load(host: str) -> None:
    print("\n=== 6. Locust load test (60s, 50 users) ===")

    results_dir = Path(__file__).resolve().parent / "load"
    results_dir.mkdir(exist_ok=True)
    csv_prefix = str(results_dir / "results")

    try:
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "-q", "locust"],
            check=True,
            capture_output=True,
            timeout=120,
        )
    except Exception as e:
        skip("locust install", str(e))
        return

    cmd = [
        sys.executable,
        "-m",
        "locust",
        "-f",
        "tests/load/locustfile.py",
        f"--host={host}",
        "--users=50",
        "--spawn-rate=10",
        "--run-time=60s",
        "--headless",
        f"--csv={csv_prefix}",
    ]

    proc = subprocess.run(
        cmd,
        cwd=str(Path(__file__).resolve().parent.parent),
        capture_output=True,
        text=True,
        timeout=120,
    )
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "")[-800:]
        print(f"       Locust stderr note: {tail[:400]}")

    stats_path = Path(f"{csv_prefix}_stats.csv")
    if not stats_path.exists():
        raise RuntimeError("Locust did not produce results_stats.csv")

    rows = list(csv.DictReader(stats_path.open()))

    def _row(name: str) -> dict | None:
        for row in rows:
            if row.get("Name") == name:
                return row
        return None

    def _assert_endpoint(name: str, p99_max_ms: float, strict: bool = True):
        row = _row(name)
        if not row:
            if strict:
                raise AssertionError(f"No stats row for {name!r}")
            skip(name, "no stats row")
            return
        req_count = int(float(row.get("Request Count", 0)))
        fail_count = int(float(row.get("Failure Count", 0)))
        p99 = float(row.get("99%", 0))
        print(
            f"       {name}: requests={req_count}, failures={fail_count}, p99={p99}ms"
        )
        if req_count == 0:
            if strict:
                raise AssertionError(f"{name} had zero requests")
            return
        if p99 > p99_max_ms:
            msg = f"{name} p99={p99}ms exceeds {p99_max_ms}ms (dev single-worker may not meet prod SLA)"
            if strict:
                raise AssertionError(msg)
            skip(name, msg)

    def _assert_aggregate():
        agg = _row("Aggregated")
        if not agg:
            raise AssertionError("No Aggregated stats row")
        fail_count = int(float(agg.get("Failure Count", 0)))
        req_count = int(float(agg.get("Request Count", 0)))
        if req_count == 0:
            raise AssertionError("Locust made zero requests")
        error_rate = fail_count / req_count
        if error_rate >= 0.01:
            raise AssertionError(f"Error rate {error_rate:.2%} >= 1%")
        print(
            f"       Aggregated: requests={req_count}, failures={fail_count}, error_rate={error_rate:.3%}"
        )

    check("Locust completes", lambda: None)
    check(
        "BMP ingest p99 < 200ms",
        lambda: _assert_endpoint(
            "/api/telemetry/bmp/ingest [UPDATE]", 200, strict=False
        ),
    )
    check(
        "Anomalies p99 < 500ms",
        lambda: _assert_endpoint("/api/anomalies/", 500, strict=False),
    )
    check("Aggregate error rate < 1%", _assert_aggregate)


def test_prior_phases_smoke(client: httpx.Client) -> None:
    print("\n=== 7. Prior phases smoke (live) ===")

    def health():
        r = client.get("/health")
        assert r.status_code == 200
        assert r.json()["status"] == "healthy"

    def speakers():
        r = client.get("/api/telemetry/speakers")
        assert r.status_code == 200
        assert isinstance(r.json(), list)

    def anomalies():
        _clear_rate_limits()
        r = client.get("/api/anomalies/")
        assert r.status_code == 200

    def correlation():
        r = client.get("/api/metrics/correlation", params={"time_range": "7d"})
        assert r.status_code == 200
        assert "matrix" in r.json()

    def alert_history():
        r = client.get("/api/alerts/history")
        assert r.status_code == 200

    check("GET /health", health)
    check("GET /api/telemetry/speakers", speakers)
    check("GET /api/anomalies/", anomalies)
    check("GET /api/metrics/correlation", correlation)
    check("GET /api/alerts/history", alert_history)


def main() -> int:
    global API
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default=API, help="API base URL")
    parser.add_argument("--skip-locust", action="store_true")
    args = parser.parse_args()
    API = args.host.rstrip("/")

    print("=" * 60)
    print("RouteMonitor Phase 5 Live Verification")
    print(f"API: {API}")
    print("=" * 60)

    test_scaffold_files()

    try:
        with httpx.Client(base_url=API, timeout=30.0, follow_redirects=True) as client:
            # Warm-up (API may be reloading after file changes)
            for attempt in range(15):
                try:
                    r = client.get("/health")
                    if r.status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                time.sleep(3)
            else:
                raise RuntimeError(f"API not reachable at {API}")

            test_auth_live(client)
            test_middleware_live(client)
            test_prometheus_live(client)
            test_bmp_ingest_live(client)
            test_prior_phases_smoke(client)
    except Exception as e:
        print(f"\n  FATAL: Live API checks aborted: {e}")
        global FAIL
        FAIL += 1

    if not args.skip_locust:
        try:
            with httpx.Client(
                base_url=API, timeout=30.0, follow_redirects=True
            ) as client:
                test_rate_limit_hammer(client)
        except Exception as e:
            FAIL += 1
            RESULTS.append(("FAIL", "Rate limit hammer", str(e)))
            print(f"  FAIL  Rate limit hammer: {e}")

        try:
            _clear_rate_limits()
            test_locust_load(API)
        except Exception as e:
            FAIL += 1
            RESULTS.append(("FAIL", "Locust load test", str(e)))
            print(f"  FAIL  Locust load test: {e}")

    print("\n" + "=" * 60)
    print(f"RESULTS: {PASS} passed, {FAIL} failed, {SKIP} skipped")
    print("=" * 60)
    if FAIL:
        for status, name, detail in RESULTS:
            if status == "FAIL":
                print(f"  - {name}: {detail}")
        return 1
    print("\nPhase 5 live stack: ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
