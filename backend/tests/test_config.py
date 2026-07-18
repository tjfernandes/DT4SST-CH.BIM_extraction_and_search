import pytest
from pydantic import ValidationError

# Resolução em runtime (config.OpenSearchSettings, config.OpenSearchConfigurationError):
# referências estáticas ficariam obsoletas após importlib.reload(shared.config)
# nos testes de import-safety, tornando a suite dependente da ordem.
import shared.config as config

SYNTHETIC_PASSWORD = "synthetic-test-password"


def make_settings(monkeypatch, **env):
    monkeypatch.setenv("OPENSEARCH_PASSWORD", SYNTHETIC_PASSWORD)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    return config.OpenSearchSettings()


def test_host_bare(monkeypatch):
    settings = make_settings(monkeypatch, OPENSEARCH_HOST="os.example.test")
    assert settings.effective_host == "os.example.test"
    assert settings.effective_scheme == "https"
    assert settings.effective_port == 9200


def test_host_http_scheme(monkeypatch):
    settings = make_settings(monkeypatch, OPENSEARCH_HOST="http://os.example.test")
    assert settings.effective_scheme == "http"
    assert settings.effective_host == "os.example.test"


def test_host_https_scheme(monkeypatch):
    settings = make_settings(monkeypatch, OPENSEARCH_HOST="https://os.example.test")
    assert settings.effective_scheme == "https"
    assert settings.effective_host == "os.example.test"


def test_explicit_port(monkeypatch):
    settings = make_settings(monkeypatch, OPENSEARCH_HOST="https://os.example.test:9243")
    assert settings.effective_port == 9243

    monkeypatch.setenv("OPENSEARCH_HOST", "os.example.test")
    monkeypatch.setenv("OPENSEARCH_PORT", "9243")
    settings = config.OpenSearchSettings()
    assert settings.effective_host == "os.example.test"
    assert settings.effective_port == 9243


def test_ssl_explicit(monkeypatch):
    settings = make_settings(
        monkeypatch,
        OPENSEARCH_HOST="http://os.example.test",
        OPENSEARCH_USE_SSL="true",
    )
    assert settings.effective_use_ssl is True

    monkeypatch.setenv("OPENSEARCH_HOST", "https://os.example.test")
    monkeypatch.setenv("OPENSEARCH_USE_SSL", "false")
    assert config.OpenSearchSettings().effective_use_ssl is False


def test_ssl_derived_from_scheme(monkeypatch):
    settings = make_settings(monkeypatch, OPENSEARCH_HOST="https://os.example.test")
    assert settings.effective_use_ssl is True

    monkeypatch.setenv("OPENSEARCH_HOST", "http://os.example.test")
    assert config.OpenSearchSettings().effective_use_ssl is False


def test_http_with_credentials_does_not_enable_ssl(monkeypatch):
    settings = make_settings(
        monkeypatch,
        OPENSEARCH_HOST="http://os.example.test",
        OPENSEARCH_USERNAME="example-user",
    )
    assert settings.effective_use_ssl is False


def test_verify_certs_default_true(monkeypatch):
    settings = make_settings(monkeypatch, OPENSEARCH_HOST="os.example.test")
    assert settings.verify_certs is True


def test_verify_certs_explicit_false(monkeypatch):
    settings = make_settings(
        monkeypatch,
        OPENSEARCH_HOST="os.example.test",
        OPENSEARCH_VERIFY_CERTS="false",
    )
    assert settings.verify_certs is False


def test_legacy_aliases(monkeypatch):
    monkeypatch.setenv("OPENSEARCH_PASSWORD", SYNTHETIC_PASSWORD)
    monkeypatch.setenv("OPENSEARCH_HOST", "os.example.test")
    monkeypatch.setenv("OPENSEARCH_USER", "legacy-user")
    monkeypatch.setenv("USE_SSL", "true")
    monkeypatch.setenv("VERIFY_CERTS", "false")
    monkeypatch.setenv("SSL_SHOW_WARN", "true")

    with pytest.warns(DeprecationWarning, match="OPENSEARCH_USER"):
        settings = config.OpenSearchSettings()
    assert settings.username == "legacy-user"
    assert settings.use_ssl is True
    assert settings.verify_certs is False
    assert settings.ssl_show_warn is True

    # Quando canónico e legado coexistem, o canónico prevalece.
    monkeypatch.setenv("OPENSEARCH_USERNAME", "canonical-user")
    with pytest.warns(DeprecationWarning):
        settings = config.OpenSearchSettings()
    assert settings.username == "canonical-user"


def test_secret_not_exposed(monkeypatch):
    settings = make_settings(monkeypatch, OPENSEARCH_HOST="os.example.test")
    assert SYNTHETIC_PASSWORD not in repr(settings)
    assert SYNTHETIC_PASSWORD not in str(settings)

    monkeypatch.setenv("OPENSEARCH_HOST", "http://os.example.test")
    monkeypatch.setenv("OPENSEARCH_SCHEME", "https")
    with pytest.raises(config.OpenSearchConfigurationError) as excinfo:
        config.OpenSearchSettings()
    assert SYNTHETIC_PASSWORD not in str(excinfo.value)


def test_missing_password_raises(monkeypatch):
    monkeypatch.setenv("OPENSEARCH_HOST", "os.example.test")
    with pytest.raises(ValidationError) as excinfo:
        config.OpenSearchSettings()
    assert "OPENSEARCH_PASSWORD" in str(excinfo.value)


def test_empty_password_raises(monkeypatch):
    monkeypatch.setenv("OPENSEARCH_HOST", "os.example.test")
    monkeypatch.setenv("OPENSEARCH_PASSWORD", "")
    with pytest.raises(ValidationError) as excinfo:
        config.OpenSearchSettings()
    assert "OPENSEARCH_PASSWORD" in str(excinfo.value)


def test_scheme_conflict_raises(monkeypatch):
    monkeypatch.setenv("OPENSEARCH_PASSWORD", SYNTHETIC_PASSWORD)
    monkeypatch.setenv("OPENSEARCH_HOST", "http://os.example.test")
    monkeypatch.setenv("OPENSEARCH_SCHEME", "https")
    with pytest.raises(config.OpenSearchConfigurationError, match="scheme"):
        config.OpenSearchSettings()


def test_port_conflict_raises(monkeypatch):
    monkeypatch.setenv("OPENSEARCH_PASSWORD", SYNTHETIC_PASSWORD)
    monkeypatch.setenv("OPENSEARCH_HOST", "os.example.test:9200")
    monkeypatch.setenv("OPENSEARCH_PORT", "9300")
    with pytest.raises(config.OpenSearchConfigurationError, match="porta"):
        config.OpenSearchSettings()


def test_client_built_with_timeouts_and_pure_host(monkeypatch):
    import shared.opensearch as shared_opensearch

    captured = {}

    def _fake_opensearch(**kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(shared_opensearch, "OpenSearch", _fake_opensearch)
    settings = make_settings(monkeypatch, OPENSEARCH_HOST="https://os.example.test:9243")
    shared_opensearch.build_opensearch_client(settings)

    assert captured["hosts"] == [{"host": "os.example.test", "port": 9243}]
    assert captured["use_ssl"] is True
    assert captured["verify_certs"] is True
    assert captured["ssl_show_warn"] is False
    assert captured["timeout"] == 30
    assert captured["max_retries"] == 3
    assert captured["retry_on_timeout"] is True
    assert captured["http_auth"] == ("admin", SYNTHETIC_PASSWORD)
