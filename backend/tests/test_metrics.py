from fastapi.testclient import TestClient

from tests.conftest import SYNTHETIC_API_KEY


def test_metrics_protected_by_default(make_app):
    client = TestClient(make_app())
    assert client.get("/metrics").status_code == 401
    resp = client.get("/metrics", headers={"X-API-Key": SYNTHETIC_API_KEY})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/plain")


def test_metrics_public_when_enabled(make_app):
    client = TestClient(make_app(metrics_public=True))
    assert client.get("/metrics").status_code == 200


def test_request_metrics_recorded_with_route_template(make_app):
    client = TestClient(make_app())
    client.get("/healthz")
    client.post("/chat", json={"message": "x"})  # 401 -> conta em errors_total
    text = client.get("/metrics", headers={"X-API-Key": SYNTHETIC_API_KEY}).text
    assert "http_requests_total" in text
    assert 'endpoint="/healthz"' in text
    assert 'status_code="200"' in text
    assert "http_request_duration_seconds" in text
    assert "http_errors_total" in text
    assert 'status_code="401"' in text
    assert 'endpoint="/chat"' in text
    # Labels limitados e não sensíveis: nunca a chave nem texto de query.
    assert SYNTHETIC_API_KEY not in text
    assert "olá" not in text


def test_metrics_registry_isolated_per_app(make_app):
    first_app = make_app()
    second_app = make_app()
    TestClient(first_app).get("/healthz")
    text = (
        TestClient(second_app)
        .get("/metrics", headers={"X-API-Key": SYNTHETIC_API_KEY})
        .text
    )
    assert 'endpoint="/healthz"' not in text
