"""Unit tests for production middleware."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from starlette.requests import Request
from starlette.responses import Response

from api.middleware import (
    ALERTS_DISPATCHED,
    ANOMALIES_DETECTED,
    BMP_MESSAGES_INGESTED,
    RateLimitMiddleware,
    RequestIDMiddleware,
)


@pytest.mark.unit
class TestRateLimitLimits:
    def test_bmp_ingest_limit(self):
        mw = RateLimitMiddleware(app=MagicMock())
        assert mw._get_limit("/api/telemetry/bmp/ingest") == 1000

    def test_anomalies_limit(self):
        mw = RateLimitMiddleware(app=MagicMock())
        assert mw._get_limit("/api/anomalies/") == 100

    def test_default_limit(self):
        mw = RateLimitMiddleware(app=MagicMock())
        assert mw._get_limit("/api/health") == 300


@pytest.mark.unit
class TestRateLimitDispatch:
    @pytest.mark.asyncio
    async def test_bypass_when_testing_env(self, monkeypatch):
        monkeypatch.setenv("TESTING", "1")
        mw = RateLimitMiddleware(app=MagicMock())
        request = Request({"type": "http", "client": ("127.0.0.1", 1234), "path": "/x"})
        call_next = AsyncMock(return_value=Response("ok", status_code=200))

        response = await mw.dispatch(request, call_next)

        assert response.status_code == 200
        call_next.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_returns_429_when_over_limit(self, monkeypatch):
        monkeypatch.delenv("TESTING", raising=False)

        mock_pipe = MagicMock()
        mock_pipe.execute.return_value = [None, 100, None, None]

        mock_redis = MagicMock()
        mock_redis.pipeline.return_value = mock_pipe

        mw = RateLimitMiddleware(app=MagicMock())
        scope = {
            "type": "http",
            "method": "GET",
            "path": "/api/anomalies/",
            "headers": [],
            "client": ("10.0.0.1", 1234),
            "server": ("testserver", 80),
            "scheme": "http",
            "query_string": b"",
        }
        request = Request(scope)

        with patch("redis.from_url", return_value=mock_redis):
            response = await mw.dispatch(request, AsyncMock())

        assert response.status_code == 429
        assert response.headers["Retry-After"] == "60"
        assert response.headers["X-RateLimit-Limit"] == "100"
        assert response.headers["X-RateLimit-Remaining"] == "0"

    @pytest.mark.asyncio
    async def test_adds_rate_limit_headers_on_success(self, monkeypatch):
        monkeypatch.delenv("TESTING", raising=False)

        mock_pipe = MagicMock()
        mock_pipe.execute.return_value = [None, 5, None, None]

        mock_redis = MagicMock()
        mock_redis.pipeline.return_value = mock_pipe

        mw = RateLimitMiddleware(app=MagicMock())
        scope = {
            "type": "http",
            "method": "GET",
            "path": "/api/health",
            "headers": [],
            "client": ("10.0.0.2", 1234),
            "server": ("testserver", 80),
            "scheme": "http",
            "query_string": b"",
        }
        request = Request(scope)
        call_next = AsyncMock(return_value=Response("ok", status_code=200))

        with patch("redis.from_url", return_value=mock_redis):
            response = await mw.dispatch(request, call_next)

        assert response.status_code == 200
        assert response.headers["X-RateLimit-Limit"] == "300"
        assert response.headers["X-RateLimit-Remaining"] == "294"


@pytest.mark.unit
class TestPrometheusCounters:
    def test_bmp_counter_increments(self):
        before = BMP_MESSAGES_INGESTED.labels(message_type="unit_test")._value.get()
        BMP_MESSAGES_INGESTED.labels(message_type="unit_test").inc()
        after = BMP_MESSAGES_INGESTED.labels(message_type="unit_test")._value.get()
        assert after == before + 1

    def test_anomaly_counter_increments(self):
        before = ANOMALIES_DETECTED.labels(
            anomaly_type="ROUTE_FLAP", severity="WARNING"
        )._value.get()
        ANOMALIES_DETECTED.labels(anomaly_type="ROUTE_FLAP", severity="WARNING").inc()
        after = ANOMALIES_DETECTED.labels(
            anomaly_type="ROUTE_FLAP", severity="WARNING"
        )._value.get()
        assert after == before + 1

    def test_alert_counter_increments(self):
        before = ALERTS_DISPATCHED.labels(
            alert_type="WEBHOOK", delivery_status="DELIVERED"
        )._value.get()
        ALERTS_DISPATCHED.labels(
            alert_type="WEBHOOK", delivery_status="DELIVERED"
        ).inc()
        after = ALERTS_DISPATCHED.labels(
            alert_type="WEBHOOK", delivery_status="DELIVERED"
        )._value.get()
        assert after == before + 1


@pytest.mark.unit
class TestRequestIDMiddleware:
    @pytest.mark.asyncio
    async def test_injects_request_id_header(self):
        mw = RequestIDMiddleware(app=MagicMock())
        scope = {
            "type": "http",
            "method": "GET",
            "path": "/health",
            "headers": [],
            "client": ("127.0.0.1", 1234),
            "server": ("testserver", 80),
            "scheme": "http",
            "query_string": b"",
        }
        request = Request(scope)
        call_next = AsyncMock(return_value=Response("ok", status_code=200))

        response = await mw.dispatch(request, call_next)

        assert "X-Request-ID" in response.headers
        assert len(response.headers["X-Request-ID"]) == 36
