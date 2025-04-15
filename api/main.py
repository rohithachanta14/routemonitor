"""FastAPI application entry point for RouteMonitor."""
import asyncio

import structlog
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from prometheus_client import make_asgi_app
from starlette.middleware.trustedhost import TrustedHostMiddleware

from api.auth import auth_router
from api.middleware import RateLimitMiddleware, RequestIDMiddleware
from api.routes import alerts, anomalies, health, metrics, telemetry

logger = structlog.get_logger(__name__)

# ─── App ──────────────────────────────────────────────────────────────────────

app = FastAPI(
    title="RouteMonitor",
    description=(
        "Real-Time BGP Telemetry & ML Anomaly Detection Platform. "
        "Collects BMP telemetry from BGP speakers, detects routing anomalies "
        "(route flaps, convergence delays, correlated failures), and alerts in <30 seconds."
    ),
    version="0.1.0",
    docs_url="/docs",
    redoc_url="/redoc",
)

# ─── Middleware (order: last added = outermost on request) ──────────────────────

app.add_middleware(RateLimitMiddleware)
app.add_middleware(RequestIDMiddleware)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "http://localhost:8501",
        "http://localhost:8502",
        "https://yourdomain.com",
    ],
    allow_credentials=True,
    allow_methods=["GET", "POST"],
    allow_headers=["Authorization", "Content-Type"],
)

app.add_middleware(
    TrustedHostMiddleware,
    allowed_hosts=["localhost", "127.0.0.1", "testserver", "*.yourdomain.com"],
)

# ─── Prometheus metrics endpoint ──────────────────────────────────────────────

metrics_app = make_asgi_app()
app.mount("/metrics", metrics_app)

# ─── Routers ──────────────────────────────────────────────────────────────────

app.include_router(health.router)
app.include_router(telemetry.router)
app.include_router(anomalies.router)
app.include_router(alerts.router)
app.include_router(metrics.router)
app.include_router(auth_router)

# ─── Exception handlers ───────────────────────────────────────────────────────


@app.exception_handler(Exception)
async def generic_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.error("unhandled_exception", path=request.url.path, error=str(exc))
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error", "error": str(exc)},
    )


# ─── Startup / Shutdown ───────────────────────────────────────────────────────


@app.on_event("startup")
async def on_startup() -> None:
    import os

    logger.info("routemonitor_starting", version="0.1.0")
    if os.getenv("TESTING") != "1":
        from api.bmp_server import BMPServer

        asyncio.create_task(BMPServer().start())


@app.on_event("shutdown")
async def on_shutdown() -> None:
    logger.info("routemonitor_shutting_down")
