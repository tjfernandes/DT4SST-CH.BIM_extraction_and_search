import importlib
import subprocess
import sys
from pathlib import Path

import canonical
import canonical.ids
import canonical.schema
import canonical.serialization

BACKEND = Path(__file__).resolve().parents[1]
# Importing canonical must pull in NONE of these — including dotenv / settings,
# which is how ".env is never read at import" is proven definitively.
FORBIDDEN = (
    "opensearchpy", "fastapi", "shared.config", "shared", "openai",
    "pydantic_settings", "dotenv", "api", "ingestion", "eval",
)


def test_canonical_modules_import():
    assert canonical.CANONICAL_SCHEMA_VERSION == "1.0"
    assert callable(canonical.element_id)
    assert hasattr(canonical, "ElementRecord")


def test_public_api_is_the_declared_all():
    # __init__ re-exports only; no accidental leakage of private helpers.
    exported = set(canonical.__all__)
    assert "ElementRecord" in exported and "to_jsonl" in exported and "element_id" in exported
    assert "_hash128" not in exported and "_require_finite_float" not in exported


def test_import_creates_no_socket_under_guard():
    # The autouse socket guard fails the test if any reload opens a socket.
    for module in (canonical.ids, canonical.schema, canonical.serialization, canonical):
        importlib.reload(module)


def test_canonical_does_not_import_infrastructure():
    # Definitive check in a FRESH interpreter: importing canonical must not pull
    # in OpenSearch, FastAPI, settings, openai or shared.config.
    code = (
        "import sys; import canonical; "
        "bad=[m for m in %r if m in sys.modules]; "
        "print('BAD:'+','.join(bad) if bad else 'CLEAN')" % (FORBIDDEN,)
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(BACKEND),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    # "dotenv" in FORBIDDEN makes this also prove ".env is never read at import".
    assert result.stdout.strip() == "CLEAN", result.stdout
