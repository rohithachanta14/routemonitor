"""Dashboard formatting helpers."""
from datetime import datetime
from typing import Any, Dict


SEVERITY_COLORS = {
    "INFO": "#3498db",
    "WARNING": "#f39c12",
    "CRITICAL": "#e74c3c",
}

ANOMALY_TYPE_LABELS = {
    "ROUTE_FLAP": "Route Flap",
    "PATH_INSTABILITY": "Path Instability",
    "CONVERGENCE_DELAY": "Convergence Delay",
    "UNUSUAL_CHURN": "Unusual Churn",
    "CORRELATED_FAILURE": "Correlated Failure",
}


def format_datetime(dt: str | datetime) -> str:
    """Format a datetime string for display."""
    if isinstance(dt, str):
        dt = datetime.fromisoformat(dt.replace("Z", "+00:00"))
    return dt.strftime("%Y-%m-%d %H:%M:%S UTC")


def severity_badge(severity: str) -> str:
    """Return colored HTML badge for severity."""
    color = SEVERITY_COLORS.get(severity, "#7f8c8d")
    return f'<span style="background:{color};color:white;padding:2px 8px;border-radius:4px">{severity}</span>'


def anomaly_type_label(anomaly_type: str) -> str:
    """Return human-readable label for anomaly type."""
    return ANOMALY_TYPE_LABELS.get(anomaly_type, anomaly_type)


def format_duration_seconds(seconds: float) -> str:
    """Format seconds as human-readable duration."""
    if seconds < 60:
        return f"{seconds:.1f}s"
    elif seconds < 3600:
        return f"{seconds/60:.1f}m"
    return f"{seconds/3600:.1f}h"
