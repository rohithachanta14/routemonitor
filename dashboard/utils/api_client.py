"""HTTP client for the RouteMonitor API."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import httpx

API_BASE_URL = "http://localhost:8001"
_DASHBOARD_USER = "operator"
_DASHBOARD_PASSWORD = "operator123"


class RouteMonitorClient:
    """Synchronous HTTP client for the RouteMonitor REST API."""

    def __init__(self, base_url: str = API_BASE_URL, timeout: float = 10.0) -> None:
        self.base_url = base_url.rstrip("/")
        self._client = httpx.Client(base_url=self.base_url, timeout=timeout)
        self._access_token: Optional[str] = None

    def _auth_headers(self) -> Dict[str, str]:
        if not self._access_token:
            self._login()
        return {"Authorization": f"Bearer {self._access_token}"}

    def _login(self) -> None:
        resp = self._client.post(
            "/api/auth/token",
            data={"username": _DASHBOARD_USER, "password": _DASHBOARD_PASSWORD},
        )
        resp.raise_for_status()
        self._access_token = resp.json()["access_token"]

    def list_speakers(self) -> List[Dict[str, Any]]:
        resp = self._client.get("/api/telemetry/speakers")
        resp.raise_for_status()
        return resp.json()

    def get_speaker(self, speaker_id: str) -> Dict[str, Any]:
        resp = self._client.get(f"/api/telemetry/speakers/{speaker_id}")
        resp.raise_for_status()
        return resp.json()

    def get_speaker_status(self, speaker_id: str) -> Dict[str, Any]:
        resp = self._client.get(f"/api/telemetry/speakers/{speaker_id}/status")
        resp.raise_for_status()
        return resp.json()

    def get_speaker_metrics(
        self, speaker_id: str, time_range: str = "24h"
    ) -> Dict[str, Any]:
        resp = self._client.get(
            f"/api/metrics/speaker/{speaker_id}",
            params={"time_range": time_range},
        )
        resp.raise_for_status()
        return resp.json()

    def get_route_events(
        self,
        speaker_id: Optional[str] = None,
        prefix: Optional[str] = None,
        event_type: Optional[str] = None,
        limit: int = 500,
    ) -> List[Dict[str, Any]]:
        params: Dict[str, Any] = {"limit": limit}
        if speaker_id:
            params["speaker_id"] = speaker_id
        if prefix:
            params["prefix"] = prefix
        if event_type:
            params["event_type"] = event_type
        resp = self._client.get("/api/telemetry/route-events", params=params)
        resp.raise_for_status()
        return resp.json()

    def get_route_stats(
        self,
        speaker_id: str,
        prefix: Optional[str] = None,
        time_range: str = "24h",
    ) -> Dict[str, Any]:
        params: Dict[str, Any] = {"time_range": time_range}
        if prefix:
            params["prefix"] = prefix
        resp = self._client.get(
            f"/api/telemetry/metrics/route-stats/{speaker_id}",
            params=params,
        )
        resp.raise_for_status()
        return resp.json()

    def list_anomalies(
        self,
        speaker_id: Optional[str] = None,
        severity: Optional[str] = None,
        anomaly_type: Optional[str] = None,
        time_range: str = "24h",
        acknowledged: Optional[bool] = None,
        limit: int = 200,
    ) -> List[Dict[str, Any]]:
        params: Dict[str, Any] = {"time_range": time_range, "limit": limit}
        if speaker_id:
            params["speaker_id"] = speaker_id
        if severity:
            params["severity"] = severity
        if anomaly_type:
            params["anomaly_type"] = anomaly_type
        if acknowledged is not None:
            params["acknowledged"] = str(acknowledged).lower()
        resp = self._client.get("/api/anomalies/", params=params)
        resp.raise_for_status()
        return resp.json()

    def acknowledge_anomaly(
        self, anomaly_id: str, acknowledged_by: str = "dashboard-user"
    ) -> Dict[str, Any]:
        resp = self._client.post(
            f"/api/anomalies/{anomaly_id}/acknowledge",
            json={"acknowledged_by": acknowledged_by},
            headers=self._auth_headers(),
        )
        resp.raise_for_status()
        return resp.json()

    def get_correlation(
        self, time_range: str = "7d", top_n_prefixes: int = 50
    ) -> Dict[str, Any]:
        resp = self._client.get(
            "/api/metrics/correlation",
            params={"time_range": time_range, "top_n_prefixes": top_n_prefixes},
        )
        resp.raise_for_status()
        return resp.json()

    def health_check(self) -> Dict[str, Any]:
        resp = self._client.get("/health")
        resp.raise_for_status()
        return resp.json()

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "RouteMonitorClient":
        return self

    def __exit__(self, *_) -> None:
        self.close()
