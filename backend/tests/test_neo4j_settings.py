"""HBIM-082 §33 — `Neo4jSettings` and the driver factory.

Settings validation, secret hygiene, import purity, the URI allowlist and the
fail-closed default. Every test clears the environment first, so a developer's
`.env` can never make one of these pass or fail by accident. Nothing here opens
a socket.
"""

from __future__ import annotations

import pathlib
import subprocess
import sys

import pytest
from pydantic import SecretStr

import shared.config

# Several existing suites reload ``shared.config`` on purpose (the import-safety
# and configuration tests do). Binding its classes here at import time would
# leave this module holding a stale class, and every ``pytest.raises`` below
# would start or stop matching depending on test order. Both are therefore
# resolved from the live module at call time.


def _error() -> type[Exception]:
    return shared.config.Neo4jConfigurationError


def _cls() -> type:
    return shared.config.Neo4jSettings


SECRET = "s3ttings-only-not-a-real-password"
URI = "bolt://neo4j.example.test:7687"

NEO4J_ENV = (
    "NEO4J_ENABLED", "NEO4J_URI", "NEO4J_DATABASE", "NEO4J_USERNAME", "NEO4J_PASSWORD",
    "NEO4J_ENCRYPTED", "NEO4J_CONNECTION_TIMEOUT_S", "NEO4J_ACQUISITION_TIMEOUT_S",
    "NEO4J_MAX_POOL_SIZE", "NEO4J_TRANSACTION_TIMEOUT_S", "NEO4J_QUERY_TIMEOUT_S",
    "NEO4J_WRITE_BATCH_SIZE", "NEO4J_MAX_QUERY_DEPTH", "NEO4J_MAX_RESULTS",
    "NEO4J_MAX_PATHS", "NEO4J_CLEANUP_RETAIN_PREVIOUS",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in NEO4J_ENV:
        monkeypatch.delenv(name, raising=False)


def _settings(**kwargs: object):
    base: dict[str, object] = {
        "enabled": True, "uri": URI, "username": "neo4j",
        "password": SecretStr(SECRET), "_env_file": None,
    }
    base.update(kwargs)
    return _cls()(**base)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# fail-closed default
# --------------------------------------------------------------------------- #
def test_default_is_disabled_with_no_endpoint() -> None:
    settings = _cls()(_env_file=None)  # type: ignore[call-arg]
    assert settings.enabled is False
    assert settings.uri is None
    assert settings.password is None


def test_defaults_are_the_documented_bounds() -> None:
    settings = _cls()(_env_file=None)  # type: ignore[call-arg]
    assert settings.database == "neo4j"
    assert settings.max_query_depth == 4
    assert settings.max_results == 50
    assert settings.max_paths == 25
    assert settings.cleanup_retain_previous is True


# --------------------------------------------------------------------------- #
# URI allowlist
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "uri",
    ["bolt://neo4j.example.test:7687", "bolt+s://neo4j.example.test:7687",
     "neo4j://neo4j.example.test:7687", "neo4j+s://neo4j.example.test:7687"],
)
def test_allowlisted_schemes_are_accepted(uri: str) -> None:
    assert _settings(uri=uri).uri == uri


@pytest.mark.parametrize(
    "uri",
    ["http://neo4j.example.test:7474", "https://neo4j.example.test:7473",
     "file:///etc/passwd", "ftp://neo4j.example.test", "jdbc:neo4j://x"],
)
def test_foreign_schemes_are_refused(uri: str) -> None:
    with pytest.raises(_error()):
        _settings(uri=uri)


def test_a_refused_uri_is_never_echoed_back() -> None:
    """A URI may carry userinfo, so the message must not repeat it."""
    secret_uri = "http://user:hunter2@neo4j.example.test:7474"
    with pytest.raises(_error()) as excinfo:
        _settings(uri=secret_uri)
    assert "hunter2" not in str(excinfo.value)
    assert secret_uri not in str(excinfo.value)


# --------------------------------------------------------------------------- #
# database name and bounds
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("name", ["neo4j", "hbim-graph", "Graph.One"])
def test_safe_database_names_are_accepted(name: str) -> None:
    assert _settings(database=name).database == name


@pytest.mark.parametrize(
    "name", ["ne", "1graph", "graph;DROP", "graph name", "graph/../etc", "a" * 64]
)
def test_unsafe_database_names_are_refused(name: str) -> None:
    with pytest.raises(_error()):
        _settings(database=name)


@pytest.mark.parametrize(
    ("field", "bad"),
    [("connection_timeout_s", 0.5), ("connection_timeout_s", 61.0),
     ("acquisition_timeout_s", 0.0), ("acquisition_timeout_s", 121.0),
     ("transaction_timeout_s", 0.5), ("transaction_timeout_s", 301.0),
     ("query_timeout_s", 0.1), ("query_timeout_s", 61.0),
     ("max_pool_size", 0), ("max_pool_size", 201),
     ("write_batch_size", 0), ("write_batch_size", 10001),
     ("max_results", 0), ("max_results", 201),
     ("max_paths", 0), ("max_paths", 101)],
)
def test_bounds_are_enforced(field: str, bad: object) -> None:
    with pytest.raises(_error()):
        _settings(**{field: bad})


@pytest.mark.parametrize("depth", [0, 7, -1])
def test_depth_is_a_closed_set(depth: int) -> None:
    with pytest.raises(_error()):
        _settings(max_query_depth=depth)


@pytest.mark.parametrize("depth", [1, 2, 3, 4, 5, 6])
def test_every_member_of_the_closed_depth_set_is_accepted(depth: int) -> None:
    assert _settings(max_query_depth=depth).max_query_depth == depth


# --------------------------------------------------------------------------- #
# secret hygiene
# --------------------------------------------------------------------------- #
def test_the_password_is_a_secret() -> None:
    settings = _settings()
    assert isinstance(settings.password, SecretStr)
    assert settings.password.get_secret_value() == SECRET


def test_the_password_never_appears_in_repr_or_str_or_dump() -> None:
    settings = _settings()
    for rendered in (repr(settings), str(settings), str(settings.model_dump())):
        assert SECRET not in rendered
    assert SECRET not in str(settings.model_dump_json())


def test_the_password_never_appears_in_a_validation_error() -> None:
    with pytest.raises(_error()) as excinfo:
        _settings(max_results=9999)
    assert SECRET not in str(excinfo.value)


# --------------------------------------------------------------------------- #
# import purity
# --------------------------------------------------------------------------- #
BACKEND = str(pathlib.Path(__file__).resolve().parents[1])


@pytest.mark.parametrize(
    "module",
    ["shared.config", "graph_store", "graph_store.client", "graph_store.schema",
     "graph_store.projection", "graph_store.writer", "graph_store.occurrence",
     "graph_store.manifests"],
)
def test_importing_a_module_opens_no_socket(module: str) -> None:
    """A fresh interpreter per module.

    Reloading into *this* process would rebind the module's exception classes
    and every later ``pytest.raises`` in the suite would stop matching, so the
    import is done in a subprocess where the blocked socket is the only signal.
    """
    code = (
        f"import sys; sys.path.insert(0, {BACKEND!r});"
        "import socket;"
        "_boom = lambda *a, **k: (_ for _ in ()).throw("
        "AssertionError('socket opened at import time'));"
        "socket.socket.connect = _boom; socket.create_connection = _boom;"
        f"__import__({module!r});"
        "print('OK')"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert "OK" in result.stdout, result.stderr


def test_importing_the_writer_creates_no_driver() -> None:
    """A fresh interpreter, so an already-imported module cannot mask this."""
    code = (
        f"import sys; sys.path.insert(0, {BACKEND!r});"
        "import neo4j;"
        "neo4j.GraphDatabase.driver = lambda *a, **k: (_ for _ in ()).throw("
        "AssertionError('driver created at import'));"
        "import graph_store.writer, graph_store.client;"
        "print('OK')"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert "OK" in result.stdout, result.stderr


def test_the_extractor_imports_without_any_neo4j_configuration() -> None:
    """§Network-and-import-safety — no NEO4J_* variable is needed to import."""
    code = (
        f"import sys; sys.path.insert(0, {BACKEND!r});"
        "import graph_store.schema, graph_store.occurrence, graph_store.projection;"
        "print('OK')"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True,
        env={"PATH": "", "PYTHONHASHSEED": "0"},
    )
    assert "OK" in result.stdout, result.stderr
