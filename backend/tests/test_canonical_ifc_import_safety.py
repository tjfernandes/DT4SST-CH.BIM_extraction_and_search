"""Import-safety for the HBIM-011 ingestion modules.

Proven in FRESH subprocess interpreters (the definitive, contamination-free
check — no in-process ``importlib.reload`` that would leak reloaded class
identities into other tests). Importing the modules must pull in NO OpenSearch /
FastAPI / settings / dotenv / api / eval, open no network socket, and need no
OpenSearch environment. IfcOpenShell is allowed; ``backend/canonical`` must stay
IfcOpenShell-free.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import ingestion.canonical_ifc
import ingestion.ifc_materials  # noqa: F401 — imported to prove it loads cleanly
import ingestion.ifc_spatial  # noqa: F401
import ingestion.ifc_values  # noqa: F401

BACKEND = Path(__file__).resolve().parents[1]
_NEW_MODULES = "ingestion.canonical_ifc, ingestion.ifc_spatial, ingestion.ifc_materials, ingestion.ifc_values"
FORBIDDEN = (
    "opensearchpy", "fastapi", "shared.config", "shared", "openai",
    "pydantic_settings", "dotenv", "api", "eval",
)


def _run(code: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", code], cwd=str(BACKEND), capture_output=True, text=True, timeout=60
    )


def test_public_api_present():
    assert callable(ingestion.canonical_ifc.convert_ifc_to_canonical)
    assert callable(ingestion.canonical_ifc.write_canonical_jsonl)


def test_ingestion_modules_import_no_infrastructure():
    # "dotenv" in FORBIDDEN also proves ".env is never read at import".
    code = (
        f"import sys; import {_NEW_MODULES}; "
        "bad=[m for m in %r if m in sys.modules]; "
        "print('BAD:'+','.join(bad) if bad else 'CLEAN')" % (FORBIDDEN,)
    )
    result = _run(code)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "CLEAN", result.stdout


def test_import_opens_no_network_socket():
    code = (
        "import socket\n"
        "class _Guard(socket.socket):\n"
        "    def __init__(self, family=-1, *a, **k):\n"
        "        if family in (-1, socket.AF_INET, socket.AF_INET6):\n"
        "            raise AssertionError('network socket created at import')\n"
        "        super().__init__(family, *a, **k)\n"
        "socket.socket = _Guard\n"
        f"import {_NEW_MODULES}\n"
        "print('CLEAN')\n"
    )
    result = _run(code)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "CLEAN", result.stdout


def test_import_without_opensearch_env():
    code = (
        "import os\n"
        "[os.environ.pop(k, None) for k in list(os.environ) if k.startswith('OPENSEARCH')]\n"
        f"import {_NEW_MODULES}\n"
        "print('CLEAN')\n"
    )
    result = _run(code)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "CLEAN", result.stdout


def test_canonical_stays_ifcopenshell_free():
    result = _run("import sys; import canonical; print('DIRTY' if 'ifcopenshell' in sys.modules else 'CLEAN')")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "CLEAN", result.stdout
