"""BGP telemetry ingestion and query endpoints."""
from datetime import datetime, timedelta, timezone
from typing import List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import func
from sqlalchemy.orm import Session

from api.dependencies import get_db, get_influxdb_connector
from api.models import BGPSpeaker, RouteEvent
from api.schemas import (
    BGPSpeakerRequest,
    BGPSpeakerResponse,
    RouteEventResponse,
    TelemetryMetricsResponse,
)
from core.influxdb_connector import InfluxDBConnector

router = APIRouter(prefix="/api/telemetry", tags=["telemetry"])


# ─── BMP Ingestion ────────────────────────────────────────────────────────────


@router.post("/bmp/ingest", status_code=status.HTTP_202_ACCEPTED)
async def ingest_bmp_message(
    request: Request,
    db: Session = Depends(get_db),
) -> dict:
    """Receive a raw BMP binary message and queue for async processing."""
    import os

    raw_bytes = await request.body()
    if not raw_bytes:
        raise HTTPException(status_code=400, detail="Empty body")

    from tasks.ingestion import parse_bmp_message_task

    if os.getenv("TESTING") == "1":
        parse_bmp_message_task.run(raw_bytes.hex())
        return {"status": "accepted", "task_id": "sync-test"}

    task = parse_bmp_message_task.delay(raw_bytes.hex())
    return {"status": "accepted", "task_id": task.id}


# ─── Speakers ─────────────────────────────────────────────────────────────────


@router.post(
    "/speakers", response_model=BGPSpeakerResponse, status_code=status.HTTP_201_CREATED
)
async def register_speaker(
    payload: BGPSpeakerRequest,
    db: Session = Depends(get_db),
) -> BGPSpeakerResponse:
    """Register a new BGP speaker."""
    existing = db.query(BGPSpeaker).filter_by(hostname=payload.hostname).first()
    if existing:
        raise HTTPException(
            status_code=409,
            detail=f"Speaker {payload.hostname!r} already registered",
        )
    speaker = BGPSpeaker(**payload.model_dump())
    db.add(speaker)
    db.commit()
    db.refresh(speaker)
    db.expire_all()
    return speaker


@router.get("/speakers", response_model=List[BGPSpeakerResponse])
async def list_speakers(
    db: Session = Depends(get_db),
) -> List[BGPSpeakerResponse]:
    """List all registered BGP speakers."""
    return db.query(BGPSpeaker).order_by(BGPSpeaker.hostname).all()


@router.get("/speakers/{speaker_id}", response_model=BGPSpeakerResponse)
async def get_speaker(
    speaker_id: UUID,
    db: Session = Depends(get_db),
) -> BGPSpeakerResponse:
    """Get a single speaker by ID."""
    speaker = db.query(BGPSpeaker).filter(BGPSpeaker.id == speaker_id).first()
    if not speaker:
        raise HTTPException(status_code=404, detail="Speaker not found")
    return speaker


@router.get("/speakers/{speaker_id}/status")
async def speaker_status(
    speaker_id: UUID,
    db: Session = Depends(get_db),
) -> dict:
    """Return real-time health for a BGP speaker."""
    speaker = db.query(BGPSpeaker).filter(BGPSpeaker.id == speaker_id).first()
    if not speaker:
        raise HTTPException(status_code=404, detail="Speaker not found")

    since = datetime.now(timezone.utc) - timedelta(hours=24)
    updates = (
        db.query(func.count(RouteEvent.id))
        .filter(
            RouteEvent.speaker_id == speaker_id,
            RouteEvent.event_type == "UPDATE",
            RouteEvent.timestamp >= since,
        )
        .scalar()
        or 0
    )
    withdraws = (
        db.query(func.count(RouteEvent.id))
        .filter(
            RouteEvent.speaker_id == speaker_id,
            RouteEvent.event_type == "WITHDRAW",
            RouteEvent.timestamp >= since,
        )
        .scalar()
        or 0
    )

    connected_for = 0.0
    if speaker.last_seen:
        last_seen = speaker.last_seen
        if last_seen.tzinfo is None:
            last_seen = last_seen.replace(tzinfo=timezone.utc)
        connected_for = (datetime.now(timezone.utc) - last_seen).total_seconds()

    return {
        "status": speaker.status,
        "last_seen": speaker.last_seen,
        "connected_for_seconds": connected_for,
        "routes_advertised_24h": updates,
        "routes_withdrawn_24h": withdraws,
        "current_flap_rate": float(withdraws + updates),
    }


# ─── Route Events ─────────────────────────────────────────────────────────────


@router.get("/route-events", response_model=List[RouteEventResponse])
async def query_route_events(
    speaker_id: Optional[UUID] = Query(None),
    prefix: Optional[str] = Query(None, description="CIDR prefix filter"),
    neighbor_ip: Optional[str] = Query(None),
    event_type: Optional[str] = Query(
        None, description="UPDATE | WITHDRAW | STATE_CHANGE"
    ),
    limit: int = Query(100, ge=1, le=10000),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
) -> List[RouteEventResponse]:
    """Query route events with optional filters."""
    q = db.query(RouteEvent)
    if speaker_id:
        q = q.filter(RouteEvent.speaker_id == speaker_id)
    if prefix:
        q = q.filter(RouteEvent.prefix == prefix)
    if neighbor_ip:
        q = q.filter(RouteEvent.neighbor_ip == neighbor_ip)
    if event_type:
        q = q.filter(RouteEvent.event_type == event_type.upper())
    return q.order_by(RouteEvent.timestamp.desc()).offset(offset).limit(limit).all()


# ─── Metrics ──────────────────────────────────────────────────────────────────


@router.get(
    "/metrics/route-stats/{speaker_id}", response_model=TelemetryMetricsResponse
)
async def get_route_stats(
    speaker_id: str,
    prefix: Optional[str] = Query(None),
    time_range: str = Query("24h", description="1h | 24h | 7d"),
    influx: InfluxDBConnector = Depends(get_influxdb_connector),
) -> TelemetryMetricsResponse:
    """Query InfluxDB for routing metrics for a speaker."""
    data_points = influx.query_route_stats(speaker_id, prefix, time_range)
    return TelemetryMetricsResponse(
        speaker_id=speaker_id,
        prefix=prefix,
        time_range=time_range,
        data_points=data_points,
    )


@router.get("/metrics/correlation")
async def get_correlation_matrix(
    time_range: str = Query("7d"),
    influx: InfluxDBConnector = Depends(get_influxdb_connector),
) -> dict:
    """Return prefix correlation matrix (Phase 3)."""
    raise HTTPException(status_code=501, detail="Not implemented yet — Phase 3")
