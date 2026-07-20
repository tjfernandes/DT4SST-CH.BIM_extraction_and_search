"""Regression tests for the evaluation runner's module isolation.

The runner re-imports a few production modules under a synthetic environment and
must restore them exactly afterwards — both the ``sys.modules`` entries and the
parent-package attributes — otherwise ``importlib.reload`` of the originals (used
by test_import_safety) raises "module ... not in sys.modules". These tests pin
that behaviour and never touch Docker or the network.
"""

import importlib
import sys

import pytest

# Imported so the affected production modules are present in sys.modules before
# the isolation window; referenced by name only via MODULES / sys.modules.
import api.search  # noqa: F401
import ingestion.index_to_opensearch  # noqa: F401
import shared.config  # noqa: F401
import shared.opensearch  # noqa: F401
from eval import run_eval
from tests import test_import_safety as _import_safety

MODULES = (
    "shared.config",
    "shared.opensearch",
    "ingestion.index_to_opensearch",
    "api.search",
)


@pytest.fixture(autouse=True)
def _resync_production_modules():
    """Reload the full production chain in dependency order after each test.

    These tests deliberately call importlib.reload(shared.config)/... to prove
    reload works post-isolation; reload redefines module-level classes
    (e.g. ApiConfigurationError) in place, so consumers that did
    ``from shared.config import X`` would hold a stale class. Reloading the whole
    chain in order re-syncs every consumer — exactly what test_import_safety does
    — so no desync leaks to later tests regardless of execution order.
    """
    yield
    for module in _import_safety._PRODUCTION_MODULES_IN_DEPENDENCY_ORDER:
        importlib.reload(module)


def _originals() -> dict[str, object]:
    return {name: sys.modules[name] for name in MODULES}


def _assert_identity_restored(originals: dict[str, object]) -> None:
    for name, obj in originals.items():
        assert sys.modules[name] is obj, f"sys.modules[{name!r}] identity lost"
        parent_name, _, attr = name.rpartition(".")
        assert getattr(sys.modules[parent_name], attr) is obj, (
            f"parent attribute {parent_name}.{attr} identity lost"
        )


def test_isolation_restores_module_and_parent_identity():
    originals = _originals()
    state = run_eval._snapshot_module_state()
    try:
        run_eval._import_production()  # pop + fresh import -> different objects
        assert sys.modules["api.search"] is not originals["api.search"]
        assert sys.modules["shared.config"] is not originals["shared.config"]
    finally:
        run_eval._restore_module_state(state)

    _assert_identity_restored(originals)
    # The exact failure reported: importlib.reload of the originals must work.
    importlib.reload(shared.config)
    importlib.reload(api.search)
    # And a name reached through the parent package still matches sys.modules,
    # which is precisely what importlib.reload checks.
    assert sys.modules["api.search"] is api.search
    assert sys.modules["shared.config"] is shared.config


def test_isolation_restores_state_even_on_exception():
    originals = _originals()
    state = run_eval._snapshot_module_state()
    try:
        run_eval._import_production()
        raise RuntimeError("boom inside the isolated window")
    except RuntimeError:
        pass
    finally:
        run_eval._restore_module_state(state)

    _assert_identity_restored(originals)
    importlib.reload(api.search)  # would raise "not in sys.modules" if broken


def test_snapshot_removes_modules_absent_before():
    # A module truly absent before the isolated window (no sys.modules entry AND
    # no parent attribute) must be removed afterwards, leaving no eval state.
    outer_state = run_eval._snapshot_module_state()  # to restore reality at the end
    try:
        # Simulate genuine absence of api.search (entry + parent attribute).
        sys.modules.pop("api.search", None)
        if hasattr(sys.modules["api"], "search"):
            delattr(sys.modules["api"], "search")

        state = run_eval._snapshot_module_state()
        assert state["api.search"] == (None, run_eval._ABSENT)
        try:
            run_eval._import_production()
            assert "api.search" in sys.modules  # freshly imported during the window
        finally:
            run_eval._restore_module_state(state)

        assert "api.search" not in sys.modules
        assert not hasattr(sys.modules["api"], "search")
    finally:
        # Bring the real modules back so later tests see live objects.
        run_eval._restore_module_state(outer_state)
        importlib.import_module("api.search")
    assert sys.modules["api"].search is sys.modules["api.search"]
