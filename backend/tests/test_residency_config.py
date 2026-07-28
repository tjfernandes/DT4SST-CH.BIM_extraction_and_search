"""HBIM-032 §10 — typed residency/ops configuration.

Every test clears the residency environment explicitly and uses
``_env_file=None``, so no developer ``.env`` can make a case pass or fail.
"""

from __future__ import annotations

import pytest

# Runtime resolution (config.ResidencySettings, config.ResidencyConfigurationError):
# static references would go stale after importlib.reload(shared.config) in the
# import-safety suites, making this file order-dependent — the same convention
# tests/test_config.py already documents.
import shared.config as config

_RESIDENCY_ENV = (
    "RESIDENCY_VRAM_TOTAL_MIB",
    "RESIDENCY_VRAM_RESERVE_MIB",
    "RESIDENCY_VRAM_BUDGET_MIB",
    "RESIDENCY_MEASUREMENT_MAX_AGE_S",
    "RESIDENCY_RECONCILIATION_TOLERANCE_MIB",
    "RESIDENCY_ACTION_TIMEOUT_S",
    "RESIDENCY_TRANSITION_TIMEOUT_S",
    "RESIDENCY_EXCLUSIVE_LOCK_TIMEOUT_S",
    "OPS_ENDPOINT_ENABLED",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _RESIDENCY_ENV:
        monkeypatch.delenv(name, raising=False)


def settings(**kwargs: object) -> "config.ResidencySettings":
    return config.ResidencySettings(_env_file=None, **kwargs)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# Defaults and derivation
# --------------------------------------------------------------------------- #
def test_defaults_match_the_specification() -> None:
    config = settings()
    assert config.vram_total_mib is None
    assert config.vram_budget_mib is None
    assert config.vram_reserve_mib == 10240
    assert config.measurement_max_age_s == 30.0
    assert config.reconciliation_tolerance_mib == 512
    assert config.action_timeout_s == 60.0
    assert config.transition_timeout_s == 120.0
    assert config.exclusive_lock_timeout_s == 300.0


def test_budget_derives_from_a_measured_total() -> None:
    # Hand-computed: 97887 - 10240 = 87647 (the spec §10 worked example).
    assert settings().budget_mib(measured_total_mib=97887) == 87647


def test_budget_derives_from_a_configured_total_without_measurement() -> None:
    assert settings(vram_total_mib=97887).budget_mib() == 87647


def test_explicit_budget_overrides_derivation_and_ignores_the_total() -> None:
    config = settings(vram_total_mib=97887, vram_budget_mib=80000)
    assert config.budget_mib() == 80000
    assert config.budget_mib(measured_total_mib=12345) == 80000


def test_budget_without_any_total_fails_closed() -> None:
    with pytest.raises(config.ResidencyConfigurationError, match="no VRAM total"):
        settings().budget_mib()


def test_total_not_exceeding_reserve_is_rejected() -> None:
    with pytest.raises(config.ResidencyConfigurationError):
        settings(vram_total_mib=10240)
    with pytest.raises(config.ResidencyConfigurationError):
        settings(vram_total_mib=1024)
    with pytest.raises(config.ResidencyConfigurationError, match="must exceed"):
        settings(vram_reserve_mib=90000).budget_mib(measured_total_mib=50000)


# --------------------------------------------------------------------------- #
# The bool trap and numeric rejection
# --------------------------------------------------------------------------- #
def test_bool_is_rejected_for_every_numeric_field() -> None:
    for field in (
        "vram_total_mib",
        "vram_reserve_mib",
        "vram_budget_mib",
        "reconciliation_tolerance_mib",
        "measurement_max_age_s",
        "action_timeout_s",
        "transition_timeout_s",
        "exclusive_lock_timeout_s",
    ):
        with pytest.raises(config.ResidencyConfigurationError, match="bool"):
            settings(**{field: True})


def test_zero_and_negative_are_rejected_where_the_spec_requires() -> None:
    for field in ("vram_total_mib", "vram_budget_mib"):
        with pytest.raises(config.ResidencyConfigurationError):
            settings(**{field: 0})
        with pytest.raises(config.ResidencyConfigurationError):
            settings(**{field: -1})
    with pytest.raises(config.ResidencyConfigurationError):
        settings(vram_reserve_mib=-1)
    for field in ("measurement_max_age_s", "action_timeout_s", "transition_timeout_s"):
        with pytest.raises(config.ResidencyConfigurationError):
            settings(**{field: 0.0})
        with pytest.raises(config.ResidencyConfigurationError):
            settings(**{field: -1.0})


def test_nan_and_infinity_are_rejected() -> None:
    for bad in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(config.ResidencyConfigurationError):
            settings(measurement_max_age_s=bad)
        with pytest.raises(config.ResidencyConfigurationError):
            settings(vram_total_mib=bad)


def test_non_integral_mib_is_rejected() -> None:
    with pytest.raises(config.ResidencyConfigurationError):
        settings(vram_total_mib=97887.5)
    # an integral float is accepted and normalised to int
    assert settings(vram_total_mib=97887.0).vram_total_mib == 97887


def test_absurd_upper_bounds_are_rejected() -> None:
    with pytest.raises(config.ResidencyConfigurationError):
        settings(vram_total_mib=1 << 30)
    with pytest.raises(config.ResidencyConfigurationError):
        settings(action_timeout_s=1e9)


# --------------------------------------------------------------------------- #
# Environment binding and safety
# --------------------------------------------------------------------------- #
def test_environment_aliases_bind(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RESIDENCY_VRAM_TOTAL_MIB", "65536")
    monkeypatch.setenv("RESIDENCY_VRAM_RESERVE_MIB", "8192")
    bound = config.ResidencySettings(_env_file=None)
    assert bound.budget_mib() == 65536 - 8192


def test_settings_are_frozen() -> None:
    from pydantic import ValidationError

    config = settings()
    with pytest.raises(ValidationError):
        config.vram_reserve_mib = 1  # type: ignore[misc]


def test_repr_carries_no_host_identifying_value() -> None:
    rendered = repr(settings(vram_total_mib=97887))
    for forbidden in ("/home/", "hbim-reranker", "127.0.0.1", "http"):
        assert forbidden not in rendered


# --------------------------------------------------------------------------- #
# Ops settings: default-off
# --------------------------------------------------------------------------- #
def test_ops_endpoint_is_disabled_by_default() -> None:
    assert config.OpsSettings(_env_file=None).enabled is False


def test_ops_endpoint_can_be_enabled_explicitly(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPS_ENDPOINT_ENABLED", "1")
    assert config.OpsSettings(_env_file=None).enabled is True
