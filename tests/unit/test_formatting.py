"""Unit tests for dashboard formatting helpers."""
from datetime import datetime, timezone

import pytest

from dashboard.utils.formatting import (
    anomaly_type_label,
    format_datetime,
    format_duration_seconds,
    severity_badge,
)


class TestFormatDatetime:
    def test_iso_string_with_z(self):
        result = format_datetime("2024-06-01T12:30:00Z")
        assert "2024-06-01" in result
        assert "UTC" in result

    def test_datetime_object(self):
        dt = datetime(2024, 6, 1, 12, 30, 0, tzinfo=timezone.utc)
        assert format_datetime(dt) == "2024-06-01 12:30:00 UTC"


class TestSeverityBadge:
    def test_known_severity(self):
        html = severity_badge("CRITICAL")
        assert "CRITICAL" in html
        assert "#e74c3c" in html

    def test_unknown_severity_fallback(self):
        html = severity_badge("UNKNOWN")
        assert "UNKNOWN" in html
        assert "#7f8c8d" in html


class TestAnomalyTypeLabel:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("ROUTE_FLAP", "Route Flap"),
            ("CORRELATED_FAILURE", "Correlated Failure"),
            ("CUSTOM_TYPE", "CUSTOM_TYPE"),
        ],
    )
    def test_labels(self, raw, expected):
        assert anomaly_type_label(raw) == expected


class TestFormatDuration:
    def test_seconds(self):
        assert format_duration_seconds(30.5) == "30.5s"

    def test_minutes(self):
        assert format_duration_seconds(90.0) == "1.5m"

    def test_hours(self):
        assert format_duration_seconds(7200.0) == "2.0h"
