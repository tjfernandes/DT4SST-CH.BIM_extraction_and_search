import logging

import pytest
from fastapi.testclient import TestClient

# Resolução em runtime (config.*) — referências estáticas ficariam obsoletas
# após importlib.reload nos testes de import-safety.
import shared.config as config
from tests.conftest import SYNTHETIC_API_KEY


def test_valid_key_returns_200(make_app, fake_llm):
    client = TestClient(make_app())
    resp = client.post(
        "/chat", json={"message": "olá"}, headers={"X-API-Key": SYNTHETIC_API_KEY}
    )
    assert resp.status_code == 200
    assert resp.json()["response"] == "resposta final"


def test_invalid_key_returns_401_with_schema(make_app):
    client = TestClient(make_app())
    resp = client.post(
        "/chat", json={"message": "olá"}, headers={"X-API-Key": "wrong-key-0000000000"}
    )
    assert resp.status_code == 401
    body = resp.json()
    assert body["error"]["code"] == "unauthorized"
    assert body["error"]["message"] == "Missing or invalid API key."
    assert "request_id" in body["error"]
    assert "wrong-key-0000000000" not in resp.text
    assert SYNTHETIC_API_KEY not in resp.text


def test_missing_key_returns_401(make_app):
    client = TestClient(make_app())
    resp = client.post("/chat", json={"message": "olá"})
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "unauthorized"


def test_auth_disabled_allows_access_and_warns(make_app, fake_llm, caplog):
    app = make_app(auth_enabled=False, api_keys=[])
    with caplog.at_level(logging.WARNING):
        with TestClient(app) as client:
            resp = client.post("/chat", json={"message": "olá"})
    assert resp.status_code == 200
    assert any("API_AUTH_ENABLED" in record.getMessage() for record in caplog.records)


def test_auth_enabled_without_keys_is_config_error(make_app, caplog):
    # Sem lifespan (sem context manager): a violação dispara no primeiro uso.
    client = TestClient(make_app(api_keys=[]))
    with caplog.at_level(logging.ERROR):
        resp = client.post(
            "/chat", json={"message": "olá"}, headers={"X-API-Key": "anything-123456"}
        )
    assert resp.status_code == 500
    assert resp.json()["error"]["code"] == "internal_error"
    # Nunca acesso silencioso; o nome da variável aparece apenas nos logs.
    assert any("API_KEYS" in record.getMessage() for record in caplog.records)
    assert "API_KEYS" not in resp.text


def test_auth_enabled_without_keys_refuses_startup(make_app):
    app = make_app(api_keys=[])
    with pytest.raises(config.ApiConfigurationError):
        with TestClient(app):
            pass


def test_key_comparison_uses_compare_digest(make_app, monkeypatch):
    import shared.security as security

    calls = {"count": 0}
    real_compare = security.hmac.compare_digest

    def _spy(a, b):
        calls["count"] += 1
        return real_compare(a, b)

    monkeypatch.setattr(security.hmac, "compare_digest", _spy)
    client = TestClient(make_app())
    resp = client.post(
        "/chat", json={"message": "olá"}, headers={"X-API-Key": "candidate-key-123456"}
    )
    assert resp.status_code == 401
    assert calls["count"] >= 1
    assert "candidate-key-123456" not in resp.text


def test_secret_not_exposed_in_settings(make_app):
    settings = config.ApiSettings(_env_file=None, api_keys=[SYNTHETIC_API_KEY])
    blob = repr(settings) + str(settings) + settings.model_dump_json()
    assert SYNTHETIC_API_KEY not in blob


def test_empty_api_key_element_rejected():
    with pytest.raises(config.ApiConfigurationError):
        config.ApiSettings(_env_file=None, api_keys=[SYNTHETIC_API_KEY, ""])
