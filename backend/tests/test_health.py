from fastapi.testclient import TestClient


class FakeChecker:
    def __init__(self, checks):
        self._checks = checks

    def check(self):
        return dict(self._checks)


def _client_with_checker(make_app, checks):
    import api.health as api_health

    app = make_app()
    app.dependency_overrides[api_health.get_readiness_checker] = lambda: FakeChecker(
        checks
    )
    return TestClient(app)


def test_healthz_public_and_static(make_app):
    client = TestClient(make_app())
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_readyz_ready(make_app):
    client = _client_with_checker(make_app, {"config": "ok", "opensearch": "ok"})
    resp = client.get("/readyz")
    assert resp.status_code == 200
    # HBIM-082 §72 — the graph state is REPORTED, never part of the verdict:
    # an optional route that was never enabled cannot make a deployment 503.
    assert resp.json() == {
        "status": "ready",
        "checks": {"config": "ok", "opensearch": "ok"},
        "graph": "disabled",
    }


def test_readyz_not_ready_exposes_no_internals(make_app):
    client = _client_with_checker(
        make_app, {"config": "ok", "opensearch": "unavailable"}
    )
    resp = client.get("/readyz")
    assert resp.status_code == 503
    body = resp.json()
    assert body == {
        "status": "not_ready",
        "checks": {"config": "ok", "opensearch": "unavailable"},
        "graph": "disabled",
    }
    assert "example.test" not in resp.text
    assert "Traceback" not in resp.text


def test_health_alias_deprecated(make_app):
    app = make_app()
    client = TestClient(app)
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
    assert app.openapi()["paths"]["/health"]["get"]["deprecated"] is True
