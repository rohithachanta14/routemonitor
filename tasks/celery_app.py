"""Celery application initialization.

Broker:  Redis (CELERY_BROKER_URL)
Backend: Redis (CELERY_RESULT_BACKEND)

Periodic tasks (celery beat):
  detect_anomalies_task  — every 5 minutes, per speaker
  compute_aggregates_task — every 5 minutes (InfluxDB rollup)
"""
import os

from celery import Celery
from celery.schedules import crontab

from core.config import settings

_TESTING = os.getenv("TESTING") == "1"

app = Celery(
    "routemonitor",
    # In TESTING mode there is no real Redis broker available; the in-memory
    # transport lets task_always_eager execute chained .delay() calls
    # in-process without Celery's apply_async trying to open a real socket.
    broker="memory://" if _TESTING else settings.CELERY_BROKER_URL,
    backend="cache+memory://" if _TESTING else settings.CELERY_RESULT_BACKEND,
    include=["tasks.ingestion"],
)

app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    task_track_started=True,
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    task_always_eager=_TESTING,
    task_eager_propagates=_TESTING,
)

# ─── Periodic task schedule ───────────────────────────────────────────────────

app.conf.beat_schedule = {
    # Fan out anomaly detection to every known speaker every 5 minutes
    "detect-anomalies-all": {
        "task": "tasks.ingestion.detect_anomalies_all_task",
        "schedule": 300.0,  # seconds
    },
    # Compute 5-min InfluxDB aggregates
    "compute-aggregates-every-5-min": {
        "task": "tasks.ingestion.compute_aggregates_task",
        "schedule": 300.0,
    },
}
