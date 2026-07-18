import pytest
from fastapi.testclient import TestClient

import shared.config as config
from tests.conftest import SYNTHETIC_API_KEY

ALLOWED_ORIGIN = "http://frontend.example.test"


def test_allowed_origin_echoed(make_app):
    client = TestClient(make_app())
    resp = client.get("/healthz", headers={"Origin": ALLOWED_ORIGIN})
    assert resp.headers.get("access-control-allow-origin") == ALLOWED_ORIGIN


def test_preflight_allows_configured_origin_and_headers(make_app):
    client = TestClient(make_app())
    resp = client.options(
        "/chat",
        headers={
            "Origin": ALLOWED_ORIGIN,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type,x-api-key",
        },
    )
    assert resp.status_code == 200
    assert resp.headers.get("access-control-allow-origin") == ALLOWED_ORIGIN
    allow_headers = (resp.headers.get("access-control-allow-headers") or "").lower()
    assert "x-api-key" in allow_headers
    expose_headers = (resp.headers.get("access-control-expose-headers") or "").lower()
    # expose-headers aplica-se às respostas simples; no preflight pode não vir.
    if expose_headers:
        assert "x-request-id" in expose_headers


def test_unconfigured_origin_not_echoed(make_app):
    client = TestClient(make_app())
    resp = client.get("/healthz", headers={"Origin": "http://evil.example.test"})
    assert "access-control-allow-origin" not in resp.headers


def test_expose_headers_includes_request_id(make_app):
    client = TestClient(make_app())
    resp = client.get("/healthz", headers={"Origin": ALLOWED_ORIGIN})
    expose = (resp.headers.get("access-control-expose-headers") or "").lower()
    assert "x-request-id" in expose


def test_wildcard_with_credentials_rejected():
    with pytest.raises(config.ApiConfigurationError):
        config.ApiSettings(
            _env_file=None,
            api_keys=[SYNTHETIC_API_KEY],
            cors_allow_origins=["*"],
            cors_allow_credentials=True,
        )


def test_list_parsing_accepts_csv_and_json():
    csv_settings = config.ApiSettings(
        _env_file=None,
        api_keys=[SYNTHETIC_API_KEY],
        cors_allow_origins="http://a.example.test, http://b.example.test",
    )
    assert csv_settings.cors_allow_origins == [
        "http://a.example.test",
        "http://b.example.test",
    ]
    json_settings = config.ApiSettings(
        _env_file=None,
        api_keys='["synthetic-key-A-000001", "synthetic-key-B-000002"]',
    )
    assert len(json_settings.api_keys) == 2
