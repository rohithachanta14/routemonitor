"""SQLAlchemy ORM models for RouteMonitor.

All models use UUID primary keys and include created_at/updated_at timestamps.
RouteEvent is intentionally immutable (append-only event log).
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSON, UUID
from sqlalchemy.orm import DeclarativeBase, relationship


class Base(DeclarativeBase):
    pass


# ─── BGPSpeaker ───────────────────────────────────────────────────────────────


class BGPSpeaker(Base):
    """A BGP router that sends BMP telemetry to RouteMonitor.

    Status transitions:
        CONNECTED → DEGRADED → DISCONNECTED → CONNECTED (reconnect)
    """

    __tablename__ = "bgp_speakers"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    hostname = Column(String(255), unique=True, nullable=False, index=True)
    router_id = Column(String(15), nullable=False)  # IPv4 string, e.g. "10.0.0.1"
    local_asn = Column(Integer, nullable=False)
    bmp_listen_address = Column(String(21), nullable=False)  # "IP:port"
    status = Column(String(20), nullable=False, default="DISCONNECTED")
    # Allowed values: CONNECTED, DISCONNECTED, DEGRADED
    last_seen = Column(DateTime, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(
        DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    # Relationships
    route_events = relationship("RouteEvent", back_populates="speaker", lazy="dynamic")
    anomalies = relationship("Anomaly", back_populates="speaker", lazy="dynamic")

    __table_args__ = (
        Index("idx_speakers_router_id", "router_id"),
        Index("idx_speakers_status", "status"),
    )

    def __repr__(self) -> str:
        return f"<BGPSpeaker hostname={self.hostname!r} asn={self.local_asn} status={self.status!r}>"


# ─── RouteEvent ───────────────────────────────────────────────────────────────


class RouteEvent(Base):
    """Immutable append-only log of every BGP route change.

    event_type values:
        UPDATE       – route advertised / path changed
        WITHDRAW     – route withdrawn
        STATE_CHANGE – peer session up/down
    """

    __tablename__ = "route_events"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    speaker_id = Column(
        UUID(as_uuid=True), ForeignKey("bgp_speakers.id"), nullable=False
    )
    timestamp = Column(DateTime, nullable=False)  # UTC, from BMP message
    event_type = Column(String(20), nullable=False)  # UPDATE | WITHDRAW | STATE_CHANGE
    prefix = Column(String(50), nullable=True)  # CIDR e.g. "10.0.0.0/24"
    path_attributes = Column(JSON, nullable=True)
    # JSON shape: {as_path: [int], next_hop: str, med: int, local_pref: int, origin: str, ...}
    withdrawn_prefixes = Column(JSON, nullable=True)  # List[str] for bulk withdrawals
    neighbor_ip = Column(String(45), nullable=False)  # IPv4 or IPv6
    neighbor_asn = Column(Integer, nullable=False)
    sequence_number = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    speaker = relationship("BGPSpeaker", back_populates="route_events")

    __table_args__ = (
        Index("idx_route_events_speaker_ts", "speaker_id", "timestamp"),
        Index("idx_route_events_prefix_ts", "prefix", "timestamp"),
        Index("idx_route_events_neighbor", "neighbor_ip", "timestamp"),
        Index("idx_route_events_event_type", "event_type"),
    )

    def __repr__(self) -> str:
        return (
            f"<RouteEvent type={self.event_type!r} prefix={self.prefix!r} "
            f"ts={self.timestamp}>"
        )


# ─── Anomaly ──────────────────────────────────────────────────────────────────


class Anomaly(Base):
    """A detected routing anomaly.

    anomaly_type values:
        ROUTE_FLAP          – rapid UPDATE/WITHDRAW cycling
        PATH_INSTABILITY    – AS path keeps changing
        CONVERGENCE_DELAY   – slow convergence after topology change
        UNUSUAL_CHURN       – abnormally high update rate (Z-score > threshold)
        CORRELATED_FAILURE  – multiple prefixes failing simultaneously (link failure)

    severity values:
        INFO, WARNING, CRITICAL
    """

    __tablename__ = "anomalies"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    speaker_id = Column(
        UUID(as_uuid=True), ForeignKey("bgp_speakers.id"), nullable=False
    )
    prefix = Column(String(50), nullable=True)  # null → system-wide anomaly
    neighbor_ip = Column(String(45), nullable=True)
    anomaly_type = Column(String(50), nullable=False)
    detected_at = Column(DateTime, nullable=False)
    resolved_at = Column(DateTime, nullable=True)
    severity = Column(String(20), nullable=False)  # INFO | WARNING | CRITICAL
    details = Column(JSON, nullable=True)
    # JSON shape: {z_score: float, baseline_flap_rate: float, current_flap_rate: float,
    #              affected_prefixes: [str], model: str, ...}
    acknowledged = Column(Boolean, nullable=False, default=False)
    acknowledged_by = Column(String(100), nullable=True)
    acknowledged_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    speaker = relationship("BGPSpeaker", back_populates="anomalies")
    alerts = relationship("Alert", back_populates="anomaly", lazy="dynamic")

    __table_args__ = (
        Index("idx_anomalies_speaker_ts", "speaker_id", "detected_at"),
        Index("idx_anomalies_severity", "severity"),
        Index("idx_anomalies_type", "anomaly_type"),
        Index("idx_anomalies_acknowledged", "acknowledged"),
    )

    def __repr__(self) -> str:
        return (
            f"<Anomaly type={self.anomaly_type!r} severity={self.severity!r} "
            f"prefix={self.prefix!r}>"
        )


# ─── Alert ────────────────────────────────────────────────────────────────────


class Alert(Base):
    """Delivery record for an alert notification.

    alert_type values:
        WEBHOOK, SLACK, PAGERDUTY, EMAIL

    delivery_status values:
        PENDING, DELIVERED, FAILED
    """

    __tablename__ = "alerts"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    anomaly_id = Column(UUID(as_uuid=True), ForeignKey("anomalies.id"), nullable=False)
    alert_type = Column(String(20), nullable=False)  # WEBHOOK | SLACK | PAGERDUTY
    target_url = Column(String(500), nullable=False)
    message = Column(Text, nullable=False)
    severity = Column(String(20), nullable=False)
    sent_at = Column(DateTime, nullable=True)
    delivery_status = Column(String(20), nullable=False, default="PENDING")
    retry_count = Column(Integer, nullable=False, default=0)
    last_retry_at = Column(DateTime, nullable=True)
    error_message = Column(Text, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    anomaly = relationship("Anomaly", back_populates="alerts")

    __table_args__ = (
        Index("idx_alerts_anomaly", "anomaly_id"),
        Index("idx_alerts_status", "delivery_status"),
    )

    def __repr__(self) -> str:
        return f"<Alert type={self.alert_type!r} status={self.delivery_status!r}>"


# ─── WebhookSubscription ──────────────────────────────────────────────────────


class WebhookSubscription(Base):
    """A registered webhook endpoint that receives anomaly alerts."""

    __tablename__ = "webhook_subscriptions"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    target_url = Column(String(500), nullable=False)
    severity_min = Column(String(20), nullable=False, default="WARNING")
    anomaly_types = Column(JSON, nullable=True)
    active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    def __repr__(self) -> str:
        return f"<WebhookSubscription url={self.target_url!r} active={self.active}>"
