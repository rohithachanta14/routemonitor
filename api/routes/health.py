"""Health check endpoint."""
import httpx
from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.orm import Session

from api.dependencies import get_db
from api.schemas import HealthResponse
from core.config import settings
from tasks.celery_app import app as celery_app

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthResponse)
def health_check(db: Session = Depends(get_db)) -> HealthResponse:
    """Return service health status.

    Checks:
    - PostgreSQL: try a simple query
    - Redis: ping via Celery broker
    - InfluxDB: ping endpoint
    """
    services: dict[str, str] = {}
    overall = "healthy"

    try:
        db.execute(text("SELECT 1"))
        services["db"] = "ok"
    except Exception:
        services["db"] = "error"
        overall = "unhealthy"

    try:
        ping_result = celery_app.control.ping(timeout=2.0)
        if ping_result:
            services["redis"] = "ok"
        else:
            services["redis"] = "error"
            overall = "unhealthy"
    except Exception:
        services["redis"] = "error"
        overall = "unhealthy"

    try:
        response = httpx.get(f"{settings.INFLUXDB_URL}/health", timeout=2.0)
        if response.status_code == 200:
            services["influxdb"] = "ok"
        else:
            services["influxdb"] = "error"
            overall = "unhealthy"
    except Exception:
        services["influxdb"] = "error"
        overall = "unhealthy"

    return HealthResponse(
        status=overall,
        version="0.1.0",
        services=services,
    )
