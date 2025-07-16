"""Unit tests for JWT auth helpers."""
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from jose import jwt

from api.auth import ROLE_LEVELS, create_access_token, decode_token, require_role
from core.config import Settings, settings


@pytest.mark.unit
class TestCreateAccessToken:
    def test_token_is_decodable(self):
        token = create_access_token("alice", role="operator", expires_minutes=60)
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=["HS256"])
        assert payload["sub"] == "alice"
        assert payload["role"] == "operator"
        assert "exp" in payload
        assert "iat" in payload

    def test_default_role_is_operator(self):
        token = create_access_token("bob")
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=["HS256"])
        assert payload["role"] == "operator"


@pytest.mark.unit
class TestDecodeToken:
    def test_valid_token_returns_payload(self):
        token = create_access_token("admin", role="admin")
        payload = decode_token(token)
        assert payload["sub"] == "admin"
        assert payload["role"] == "admin"

    def test_expired_token_raises_401(self):
        payload = {
            "sub": "expired-user",
            "role": "operator",
            "exp": datetime.now(timezone.utc) - timedelta(minutes=5),
        }
        token = jwt.encode(payload, settings.SECRET_KEY, algorithm="HS256")
        with pytest.raises(HTTPException) as exc:
            decode_token(token)
        assert exc.value.status_code == 401
        assert "WWW-Authenticate" in exc.value.headers

    def test_invalid_signature_raises_401(self):
        token = create_access_token("user")
        tampered = token[:-4] + "XXXX"
        with pytest.raises(HTTPException) as exc:
            decode_token(tampered)
        assert exc.value.status_code == 401

    def test_missing_sub_raises_401(self):
        payload = {
            "role": "admin",
            "exp": datetime.now(timezone.utc) + timedelta(hours=1),
        }
        token = jwt.encode(payload, settings.SECRET_KEY, algorithm="HS256")
        with pytest.raises(HTTPException) as exc:
            decode_token(token)
        assert exc.value.status_code == 401
        assert "Invalid token payload" in exc.value.detail


@pytest.mark.unit
class TestRoleHierarchy:
    def test_role_levels_ordering(self):
        assert ROLE_LEVELS["readonly"] < ROLE_LEVELS["operator"]
        assert ROLE_LEVELS["operator"] < ROLE_LEVELS["admin"]

    def test_require_role_factory_returns_callable(self):
        dep = require_role("admin")
        assert callable(dep)


@pytest.mark.unit
class TestSettingsSecretKey:
    def test_short_secret_allowed_in_development(self):
        s = Settings(APP_ENV="development", SECRET_KEY="short")
        assert s.SECRET_KEY == "short"

    def test_short_secret_rejected_in_production(self):
        with pytest.raises(ValueError, match="SECRET_KEY must be at least 32"):
            Settings(APP_ENV="production", SECRET_KEY="too-short")

    def test_long_secret_accepted_in_production(self):
        key = "a" * 32
        s = Settings(APP_ENV="production", SECRET_KEY=key)
        assert s.SECRET_KEY == key
