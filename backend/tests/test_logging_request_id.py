import json as jsonlib
import logging
import re

from fastapi.testclient import TestClient


def test_valid_inbound_id_preserved(make_app):
    client = TestClient(make_app())
    resp = client.get("/healthz", headers={"X-Request-ID": "abc-123_x.y"})
    assert resp.headers["X-Request-ID"] == "abc-123_x.y"


def test_invalid_inbound_id_replaced(make_app):
    client = TestClient(make_app())
    resp = client.get("/healthz", headers={"X-Request-ID": "ab"})
    generated = resp.headers["X-Request-ID"]
    assert re.fullmatch(r"[0-9a-f]{32}", generated)


def test_generated_when_absent(make_app):
    client = TestClient(make_app())
    resp = client.get("/healthz")
    assert re.fullmatch(r"[0-9a-f]{32}", resp.headers["X-Request-ID"])


def test_request_id_in_completion_log(make_app, caplog):
    client = TestClient(make_app())
    with caplog.at_level(logging.INFO):
        resp = client.get("/healthz", headers={"X-Request-ID": "req-id-test-0001"})
    assert resp.status_code == 200
    completion = [r for r in caplog.records if r.getMessage() == "request completed"]
    assert completion
    record = completion[-1]
    assert record.request_id == "req-id-test-0001"
    assert record.method == "GET"
    assert record.path == "/healthz"
    assert record.status_code == 200
    assert record.duration_ms >= 0


def test_error_response_carries_request_id(make_app):
    client = TestClient(make_app())
    resp = client.post(
        "/chat", json={"message": "x"}, headers={"X-Request-ID": "req-id-test-0002"}
    )
    assert resp.status_code == 401
    assert resp.headers["X-Request-ID"] == "req-id-test-0002"
    assert resp.json()["error"]["request_id"] == "req-id-test-0002"


def test_json_formatter_redacts_sensitive_fields():
    import shared.logging as shared_logging

    record = logging.LogRecord("t", logging.INFO, __file__, 1, "mensagem", (), None)
    record.api_key = "SENSITIVE-VALUE"
    record.request_id = "req-id-test-0003"
    formatted = shared_logging.JsonLogFormatter().format(record)
    data = jsonlib.loads(formatted)
    assert data["api_key"] == "***"
    assert "SENSITIVE-VALUE" not in formatted
    assert data["request_id"] == "req-id-test-0003"
    assert data["message"] == "mensagem"
    assert data["level"] == "INFO"


def test_log_preprocess_json_redacted(monkeypatch, caplog):
    import api.main as api_main

    monkeypatch.setattr(api_main, "PREPROCESS_LOG_JSONS", True)
    with caplog.at_level(logging.INFO):
        api_main.log_preprocess_json("step", {"api_key": "SENSITIVE-VALUE", "ok": 1})
    text = "\n".join(record.getMessage() for record in caplog.records)
    assert "***" in text
    assert "SENSITIVE-VALUE" not in text


def test_text_log_format(capsys):
    import shared.logging as shared_logging

    shared_logging.setup_logging("text", "INFO")
    logging.getLogger("hbim.test").info("linha de texto")
    out = capsys.readouterr().out
    assert "linha de texto" in out
    assert not out.strip().startswith("{")
