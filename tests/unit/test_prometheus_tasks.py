"""Unit tests for Prometheus counter increments in Celery tasks."""
from __future__ import annotations

import uuid
from unittest.mock import MagicMock, patch

import pytest

from api.middleware import ALERTS_DISPATCHED, ANOMALIES_DETECTED, BMP_MESSAGES_INGESTED
from tests.fixtures.bgp_telemetry_generator import MockBGPTelemetryGenerator


@pytest.mark.unit
class TestBmpIngestPrometheus:
    def test_parse_bmp_increments_counter(self, db_session):
        from api.models import BGPSpeaker
        from tasks.ingestion import parse_bmp_message_task

        speaker = BGPSpeaker(
            hostname="prom-test",
            router_id="10.0.0.1",
            local_asn=65000,
            bmp_listen_address="10.0.0.1:179",
        )
        db_session.add(speaker)
        db_session.commit()

        bmp = MockBGPTelemetryGenerator().generate_update("10.1.0.0/24", 65000)
        before = BMP_MESSAGES_INGESTED.labels(message_type="0")._value.get()

        with patch("tasks.ingestion.ingest_metrics_task") as mock_ingest:
            mock_ingest.apply.return_value = MagicMock()
            parse_bmp_message_task.run(bmp.hex())

        after = BMP_MESSAGES_INGESTED.labels(message_type="0")._value.get()
        assert after == before + 1


@pytest.mark.unit
class TestAnomalyPrometheus:
    def test_detect_anomalies_increments_counter(self, db_session, mock_speaker):
        from api.models import Anomaly
        from tasks.ingestion import detect_anomalies_task

        with (
            patch("tasks.ingestion.InfluxDBConnector") as mock_influx_cls,
            patch("tasks.ingestion.dispatch_alerts_task"),
        ):
            mock_influx = MagicMock()
            mock_influx_cls.return_value = mock_influx
            mock_influx.query_route_stats.side_effect = [
                [
                    {"flap_count": 1, "route_count": 100, "path_diversity": 2.0}
                    for _ in range(50)
                ],
                [{"flap_count": 999, "route_count": 100, "path_diversity": 2.0}],
            ]
            detect_anomalies_task.run(str(mock_speaker.id))

        created = (
            db_session.query(Anomaly)
            .filter(Anomaly.speaker_id == mock_speaker.id)
            .all()
        )
        assert len(created) >= 1
        a = created[-1]
        counter = ANOMALIES_DETECTED.labels(
            anomaly_type=a.anomaly_type,
            severity=a.severity,
        )._value.get()
        assert counter >= 1


@pytest.mark.unit
class TestAlertPrometheus:
    def test_dispatch_increments_counter_on_delivery(self, db_session, mock_anomaly):
        from api.models import WebhookSubscription
        from tasks.ingestion import dispatch_alerts_task

        sub = WebhookSubscription(
            target_url="https://example.com/hook",
            severity_min="INFO",
            anomaly_types=None,
            active=True,
        )
        db_session.add(sub)
        db_session.commit()

        before = ALERTS_DISPATCHED.labels(
            alert_type="WEBHOOK", delivery_status="DELIVERED"
        )._value.get()

        with patch("core.dispatcher.AlertDispatcher._send_webhook", return_value=True):
            dispatch_alerts_task.run(str(mock_anomaly.id))

        after = ALERTS_DISPATCHED.labels(
            alert_type="WEBHOOK", delivery_status="DELIVERED"
        )._value.get()
        assert after == before + 1

    def test_dispatch_increments_failed_counter(self, db_session, mock_anomaly):
        from api.models import WebhookSubscription
        from tasks.ingestion import dispatch_alerts_task

        sub = WebhookSubscription(
            target_url="https://example.com/hook-fail",
            severity_min="INFO",
            active=True,
        )
        db_session.add(sub)
        db_session.commit()

        before = ALERTS_DISPATCHED.labels(
            alert_type="WEBHOOK", delivery_status="FAILED"
        )._value.get()

        with patch("core.dispatcher.AlertDispatcher._send_webhook", return_value=False):
            with patch(
                "core.dispatcher.AlertDispatcher._retry_with_backoff",
                return_value=False,
            ):
                dispatch_alerts_task.run(str(mock_anomaly.id))

        after = ALERTS_DISPATCHED.labels(
            alert_type="WEBHOOK", delivery_status="FAILED"
        )._value.get()
        assert after == before + 1
