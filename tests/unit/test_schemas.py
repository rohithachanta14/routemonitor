"""Unit tests for Pydantic schemas."""
import pytest
from pydantic import ValidationError

from api.schemas import AlertWebhookRequest, BGPSpeakerRequest, RouteEventQueryParams


class TestBGPSpeakerRequest:
    def test_valid_speaker(self):
        """Valid speaker request should parse without error."""
        speaker = BGPSpeakerRequest(
            hostname="router1",
            router_id="10.0.0.1",
            local_asn=65000,
            bmp_listen_address="192.168.1.1:179",
        )
        assert speaker.hostname == "router1"
        assert speaker.router_id == "10.0.0.1"
        assert speaker.local_asn == 65000
        assert speaker.bmp_listen_address == "192.168.1.1:179"

    def test_invalid_router_id(self):
        """Non-IPv4 router_id should raise ValidationError."""
        with pytest.raises(ValidationError):
            BGPSpeakerRequest(
                hostname="router1",
                router_id="not-an-ip",
                local_asn=65000,
                bmp_listen_address="192.168.1.1:179",
            )

    def test_invalid_asn_too_large(self):
        """ASN > 4294967295 should raise ValidationError."""
        with pytest.raises(ValidationError):
            BGPSpeakerRequest(
                hostname="router1",
                router_id="10.0.0.1",
                local_asn=4294967296,
                bmp_listen_address="192.168.1.1:179",
            )

    def test_invalid_asn_zero(self):
        """ASN = 0 should raise ValidationError."""
        with pytest.raises(ValidationError):
            BGPSpeakerRequest(
                hostname="router1",
                router_id="10.0.0.1",
                local_asn=0,
                bmp_listen_address="192.168.1.1:179",
            )


class TestRouteEventQueryParams:
    def test_valid_cidr_prefix_filter(self):
        """Valid CIDR prefix should pass validation."""
        params = RouteEventQueryParams(prefix="10.0.0.0/24")
        assert params.prefix == "10.0.0.0/24"

    def test_invalid_prefix(self):
        """Non-CIDR string prefix should raise ValidationError."""
        with pytest.raises(ValidationError):
            RouteEventQueryParams(prefix="not-a-cidr")

    def test_invalid_event_type(self):
        """Unknown event_type should raise ValidationError."""
        with pytest.raises(ValidationError):
            RouteEventQueryParams(event_type="INVALID")


class TestAlertWebhookRequest:
    def test_valid_webhook(self):
        """Valid webhook URL should parse."""
        req = AlertWebhookRequest(target_url="https://example.com/webhook")
        assert req.target_url == "https://example.com/webhook"
        assert req.severity_min == "WARNING"

    def test_non_http_url_rejected(self):
        """Non-http(s) URL should raise ValidationError."""
        with pytest.raises(ValidationError):
            AlertWebhookRequest(target_url="ftp://example.com/hook")

    def test_invalid_severity_min(self):
        """Unknown severity should raise ValidationError."""
        with pytest.raises(ValidationError):
            AlertWebhookRequest(
                target_url="https://example.com/webhook",
                severity_min="LOW",
            )
