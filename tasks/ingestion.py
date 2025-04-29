"""Celery tasks for BMP ingestion, metrics, anomaly detection, and alerting."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

import structlog
from celery import shared_task

from api.database import SessionLocal
from api.middleware import ALERTS_DISPATCHED, ANOMALIES_DETECTED, BMP_MESSAGES_INGESTED
from api.models import BGPSpeaker, RouteEvent
from core.bmp_parser import BMPParser, parsed_message_to_dict
from core.config import settings
from core.influxdb_connector import InfluxDBConnector

logger = structlog.get_logger(__name__)


def _resolve_speaker(db, peer_header: dict) -> Optional[BGPSpeaker]:
    """Find a BGPSpeaker matching the BMP peer header."""
    peer_asn = peer_header.get("peer_asn")
    peer_addr = peer_header.get("peer_address", "")

    speaker = None
    if peer_asn:
        speaker = db.query(BGPSpeaker).filter(BGPSpeaker.local_asn == peer_asn).first()
    if not speaker and peer_addr:
        speaker = (
            db.query(BGPSpeaker)
            .filter(BGPSpeaker.bmp_listen_address.contains(peer_addr))
            .first()
        )
    if not speaker:
        speaker = db.query(BGPSpeaker).first()
    return speaker


@shared_task(
    name="tasks.ingestion.parse_bmp_message_task",
    bind=True,
    max_retries=3,
    default_retry_delay=5,
)
def parse_bmp_message_task(self, message_bytes_hex: str) -> dict:
    """Parse a raw BMP message and chain to metrics ingestion."""
    try:
        data = bytes.fromhex(message_bytes_hex)
        parsed = BMPParser().parse_message(data)
        result = parsed_message_to_dict(parsed)
        BMP_MESSAGES_INGESTED.labels(
            message_type=str(result.get("message_type", "unknown"))
        ).inc()
        peer_asn = result.get("peer_header", {}).get("peer_asn", 0)
        ingest_metrics_task.apply(args=[result, str(peer_asn)])
        return result
    except Exception as exc:
        logger.error("bmp_parse_failed", error=str(exc))
        raise self.retry(exc=exc)


@shared_task(
    name="tasks.ingestion.ingest_metrics_task",
    bind=True,
    max_retries=3,
    default_retry_delay=5,
)
def ingest_metrics_task(self, parsed_message: dict, speaker_id: str) -> dict:
    """Persist parsed BMP data to PostgreSQL and InfluxDB."""
    db = SessionLocal()
    influx = InfluxDBConnector(
        url=settings.INFLUXDB_URL,
        token=settings.INFLUXDB_TOKEN,
        org=settings.INFLUXDB_ORG,
        bucket=settings.INFLUXDB_BUCKET,
    )
    try:
        body = parsed_message.get("bgp_update", {})
        peer = parsed_message.get("peer_header", {})
        ts = datetime.now(timezone.utc)

        speaker = _resolve_speaker(db, peer)
        if not speaker:
            logger.warning("no_speaker_found", peer_asn=peer.get("peer_asn"))
            return {"events_created": 0, "metrics_written": 0}

        resolved_speaker_id = speaker.id
        events_created = 0

        for prefix in body.get("nlri_prefixes") or []:
            event = RouteEvent(
                speaker_id=resolved_speaker_id,
                timestamp=ts,
                event_type="UPDATE",
                prefix=prefix,
                path_attributes=body.get("path_attributes"),
                neighbor_ip=peer.get("peer_address", "0.0.0.0"),
                neighbor_asn=peer.get("peer_asn", 0),
                sequence_number=0,
            )
            db.add(event)
            events_created += 1

        for prefix in body.get("withdrawn_prefixes") or []:
            event = RouteEvent(
                speaker_id=resolved_speaker_id,
                timestamp=ts,
                event_type="WITHDRAW",
                prefix=prefix,
                withdrawn_prefixes=[prefix],
                neighbor_ip=peer.get("peer_address", "0.0.0.0"),
                neighbor_asn=peer.get("peer_asn", 0),
                sequence_number=0,
            )
            db.add(event)
            events_created += 1

        db.commit()

        metrics_written = 0
        if events_created > 0:
            influx.write_metrics_batch(
                [
                    {
                        "measurement": "route_stats",
                        "tags": {
                            "speaker_id": str(resolved_speaker_id),
                            "neighbor_ip": peer.get("peer_address", ""),
                        },
                        "fields": {
                            "flap_count": len(body.get("withdrawn_prefixes") or []),
                            "route_count": len(body.get("nlri_prefixes") or []),
                            "path_diversity": 1.0,
                        },
                    }
                ]
            )
            metrics_written = 1

        detect_anomalies_task.delay(str(resolved_speaker_id))
        return {"events_created": events_created, "metrics_written": metrics_written}
    except Exception as exc:
        db.rollback()
        logger.error("ingest_metrics_failed", error=str(exc))
        raise self.retry(exc=exc)
    finally:
        db.close()
        influx.close()


@shared_task(
    name="tasks.ingestion.compute_aggregates_task",
)
def compute_aggregates_task() -> dict:
    """Compute 5-minute aggregate metrics in InfluxDB."""
    influx = InfluxDBConnector(
        url=settings.INFLUXDB_URL,
        token=settings.INFLUXDB_TOKEN,
        org=settings.INFLUXDB_ORG,
        bucket=settings.INFLUXDB_BUCKET,
    )
    try:
        flux = f"""
from(bucket: "{influx.bucket}")
  |> range(start: -5m)
  |> filter(fn: (r) => r._measurement == "route_stats")
  |> filter(fn: (r) => r._field == "flap_count" or r._field == "route_count" or r._field == "convergence_ms")
  |> toFloat()
  |> group(columns: ["speaker_id"])
  |> pivot(rowKey:["_time"], columnKey: ["_field"], valueColumn: "_value")
"""
        tables = influx.query_api.query(flux, org=influx.org)
        speaker_data: dict[str, dict] = {}

        for table in tables:
            for record in table.records:
                sid = record.values.get("speaker_id", "unknown")
                if sid not in speaker_data:
                    speaker_data[sid] = {
                        "flap_count": 0,
                        "route_count": 0,
                        "convergence_ms": [],
                    }
                if record.values.get("flap_count") is not None:
                    speaker_data[sid]["flap_count"] += int(record.values["flap_count"])
                if record.values.get("route_count") is not None:
                    speaker_data[sid]["route_count"] += int(
                        record.values["route_count"]
                    )
                if record.values.get("convergence_ms") is not None:
                    speaker_data[sid]["convergence_ms"].append(
                        float(record.values["convergence_ms"])
                    )

        points = []
        for sid, agg in speaker_data.items():
            avg_conv = (
                sum(agg["convergence_ms"]) / len(agg["convergence_ms"])
                if agg["convergence_ms"]
                else 0.0
            )
            points.append(
                {
                    "measurement": "route_stats",
                    "tags": {"speaker_id": sid, "resolution": "5min"},
                    "fields": {
                        "flap_count": agg["flap_count"],
                        "route_count": agg["route_count"],
                        "convergence_ms": avg_conv,
                    },
                }
            )

        if points:
            influx.write_metrics_batch(points)

        return {
            "speakers_processed": len(speaker_data),
            "points_written": len(points),
        }
    finally:
        influx.close()


@shared_task(
    name="tasks.ingestion.detect_anomalies_task",
    bind=True,
    max_retries=2,
)
def detect_anomalies_task(self, speaker_id: str) -> dict:
    """Run the anomaly detection pipeline for one speaker."""
    import asyncio

    from core.detector import AnomalyDetector

    db = SessionLocal()
    influx = InfluxDBConnector(
        url=settings.INFLUXDB_URL,
        token=settings.INFLUXDB_TOKEN,
        org=settings.INFLUXDB_ORG,
        bucket=settings.INFLUXDB_BUCKET,
    )
    try:
        detector = AnomalyDetector(
            lookback_days=settings.ANOMALY_BASELINE_DAYS,
            z_score_threshold=settings.ANOMALY_Z_SCORE_THRESHOLD,
            dedup_window_seconds=settings.ANOMALY_DEDUP_WINDOW_SECONDS,
        )
        anomalies = asyncio.run(
            detector.detect_anomalies(speaker_id, influx=influx, db=db)
        )
        for a in anomalies:
            ANOMALIES_DETECTED.labels(
                anomaly_type=a["anomaly_type"],
                severity=a["severity"],
            ).inc()
        logger.info(
            "anomaly_detection_complete",
            speaker_id=speaker_id,
            count=len(anomalies),
        )
        return {"anomalies_detected": len(anomalies)}
    except Exception as exc:
        db.rollback()
        logger.error("anomaly_detection_failed", speaker_id=speaker_id, error=str(exc))
        raise self.retry(exc=exc)
    finally:
        db.close()
        influx.close()


@shared_task(
    name="tasks.ingestion.dispatch_alerts_task",
    bind=True,
    max_retries=3,
    default_retry_delay=10,
)
def dispatch_alerts_task(self, anomaly_id: str) -> dict:
    """Dispatch alert notifications for a persisted Anomaly record."""
    import asyncio
    import uuid

    import httpx

    from api.models import Anomaly
    from core.dispatcher import AlertDispatcher

    db = SessionLocal()
    try:
        anomaly = (
            db.query(Anomaly).filter(Anomaly.id == uuid.UUID(str(anomaly_id))).first()
        )
        if not anomaly:
            logger.warning("anomaly_not_found", anomaly_id=anomaly_id)
            return {"alerts_sent": 0, "alerts_failed": 0}

        anomaly_dict = {
            "id": str(anomaly.id),
            "anomaly_type": anomaly.anomaly_type,
            "severity": anomaly.severity,
            "prefix": anomaly.prefix,
            "speaker_id": str(anomaly.speaker_id),
            "detected_at": anomaly.detected_at.isoformat(),
            "details": anomaly.details or {},
        }

        async def _run():
            async with httpx.AsyncClient(timeout=10.0) as client:
                dispatcher = AlertDispatcher(db=db, http_client=client)
                try:
                    return await dispatcher.dispatch(anomaly_dict)
                finally:
                    await dispatcher.close()

        results = asyncio.run(_run())

        for r in results:
            ALERTS_DISPATCHED.labels(
                alert_type="WEBHOOK",
                delivery_status=r.get("delivery_status", "FAILED"),
            ).inc()

        sent = sum(1 for r in results if r.get("delivery_status") == "DELIVERED")
        failed = sum(1 for r in results if r.get("delivery_status") == "FAILED")
        logger.info(
            "alerts_dispatched",
            anomaly_id=anomaly_id,
            sent=sent,
            failed=failed,
        )
        return {"alerts_sent": sent, "alerts_failed": failed}
    except Exception as exc:
        logger.error("dispatch_alerts_failed", anomaly_id=anomaly_id, error=str(exc))
        raise self.retry(exc=exc)
    finally:
        db.close()
