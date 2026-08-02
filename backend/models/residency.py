"""HBIM-032 — VRAM residency manager and GPU profiles.

Closed enums, an immutable service registry, conservative VRAM accounting, a
**pure** transition planner and a capability-gated executor.

Import safety (spec §27): importing this module opens no socket, calls no
Docker API, invokes no subprocess or ``nvidia-smi``, reads no ``.env``, creates
no lock bound to an event loop and loads no model. Every effectful dependency
is injected (adapters, clock) or created lazily inside the manager.

Truthfulness (spec §7): no operation is ever silently substituted for another.
``sleep`` is not ``docker stop``; ``unloaded`` is not ``unhealthy``; ``loaded``
is not "the container exists"; a configured GPU-memory fraction is **not** a
measurement. Unsupported transitions fail closed with typed errors.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from enum import Enum
from types import MappingProxyType
from typing import TYPE_CHECKING, Mapping, Sequence

if TYPE_CHECKING:  # pragma: no cover - typing only
    import asyncio

if TYPE_CHECKING:  # pragma: no cover - typing only
    from retrieval.router import Route

__all__ = [
    "PROFILE_CATALOG",
    "Action",
    "AmbiguousOwnershipError",
    "Backend",
    "Capabilities",
    "CapabilityEvidence",
    "CapabilityUnavailableError",
    "IllegalTransitionError",
    "IrreversiblePlanError",
    "OverBudgetError",
    "OwnerRef",
    "PlanStep",
    "ProfileAvailability",
    "ProfileMember",
    "ProfileUnavailableError",
    "ProfileVerdict",
    "ReasonCode",
    "Registry",
    "ResidencyError",
    "ResidencyProfile",
    "ServiceIdentity",
    "ServiceName",
    "ServiceRecord",
    "ServiceState",
    "ServiceUnavailableError",
    "StalePlanError",
    "TransitionPlan",
    "accounted_total_mib",
    "default_registry",
    "derive_budget_mib",
    "effective_accounted_mib",
    "evaluate_profile",
    "plan_transition",
    "profile_for_route",
    "state_accounted_mib",
    "validate_mib",
]


# --------------------------------------------------------------------------- #
# Closed enums (spec §11, §12, §14, §15)
# --------------------------------------------------------------------------- #
class ServiceName(str, Enum):
    """Closed allowlist of controllable/declared service slots (spec §24)."""

    EMB_QWEN3_8B = "emb-qwen3-8b"
    RERANK_QWEN3_8B = "rerank-qwen3-8b"
    JINA_CLIP = "jina-clip"
    OCR = "ocr"
    DOCLING = "docling"
    VLM_8B = "vlm-8b"
    VLM_32B = "vlm-32b"
    COLQWEN = "colqwen"


class Backend(str, Enum):
    TEI = "tei"
    VLLM = "vllm"
    NONE = "none"


class ServiceState(str, Enum):
    """Closed state machine (spec §12)."""

    UNAVAILABLE = "unavailable"
    UNLOADED = "unloaded"
    LOADING = "loading"
    LOADED = "loaded"
    SLEEPING = "sleeping"
    WAKING = "waking"
    UNLOADING = "unloading"
    FAILED = "failed"


class CapabilityEvidence(str, Enum):
    PROVEN_LIVE = "proven_live"
    DOCUMENTED = "documented"
    UNAVAILABLE = "unavailable"


class Action(str, Enum):
    LOAD = "load"
    UNLOAD = "unload"
    SLEEP = "sleep"
    WAKE = "wake"


class ResidencyProfile(str, Enum):
    """The five roadmap profiles (spec §14)."""

    P_ONLINE_TEXT = "P-Online-Text"
    P_ONLINE_MM = "P-Online-MM"
    P_VERIFY_HARD = "P-Verify-Hard"
    P_INGEST_DOCS = "P-Ingest-Docs"
    P_INGEST_VISUAL = "P-Ingest-Visual"


class ProfileAvailability(str, Enum):
    AVAILABLE = "available"
    DEGRADED = "degraded"
    BLOCKED_BY_CAPABILITY = "blocked_by_capability"
    UNAVAILABLE = "unavailable"


class ReasonCode(str, Enum):
    """Closed reason codes (spec §26): never free text, never user content."""

    OK = "ok"
    MISSING_REQUIRED_MEMBER = "missing_required_member"
    MISSING_OPTIONAL_MEMBER = "missing_optional_member"
    MISSING_CAPABILITY = "missing_capability"
    OWNERSHIP_UNVERIFIED = "ownership_unverified"
    AMBIGUOUS_OWNERSHIP = "ambiguous_ownership"
    STALE_MEASUREMENT = "stale_measurement"
    RECONCILIATION_DRIFT = "reconciliation_drift"
    OVER_BUDGET = "over_budget"
    IRREVERSIBLE_PLAN = "irreversible_plan"
    STALE_PLAN = "stale_plan"
    ILLEGAL_TRANSITION = "illegal_transition"
    ACTION_FAILED = "action_failed"
    ACTION_TIMEOUT = "action_timeout"
    IDENTITY_MISMATCH = "identity_mismatch"
    NOT_HEALTHY = "not_healthy"
    CANCELLED = "cancelled"
    ROLLBACK_FAILED = "rollback_failed"
    RESTORATION_FAILED = "restoration_failed"
    REENTRANT = "reentrant"


# --------------------------------------------------------------------------- #
# Typed failure taxonomy (spec §16, §17, §19, §20)
# --------------------------------------------------------------------------- #
class ResidencyError(Exception):
    """Base for every residency failure. Carries a closed reason code only."""

    reason = ReasonCode.ACTION_FAILED


class ProfileUnavailableError(ResidencyError):
    reason = ReasonCode.MISSING_REQUIRED_MEMBER


class CapabilityUnavailableError(ResidencyError):
    reason = ReasonCode.MISSING_CAPABILITY


class ServiceUnavailableError(ResidencyError):
    reason = ReasonCode.MISSING_REQUIRED_MEMBER


class OverBudgetError(ResidencyError):
    reason = ReasonCode.OVER_BUDGET


class IrreversiblePlanError(ResidencyError):
    reason = ReasonCode.IRREVERSIBLE_PLAN


class StalePlanError(ResidencyError):
    reason = ReasonCode.STALE_PLAN


class IllegalTransitionError(ResidencyError):
    reason = ReasonCode.ILLEGAL_TRANSITION


class AmbiguousOwnershipError(ResidencyError):
    reason = ReasonCode.AMBIGUOUS_OWNERSHIP


# --------------------------------------------------------------------------- #
# Immutable types (spec §11)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class OwnerRef:
    """Exact ownership metadata (spec §24). Compared by equality, never by
    substring, prefix or regex."""

    project: str
    service: str
    milestone: str


@dataclass(frozen=True)
class ServiceIdentity:
    name: ServiceName
    model_id: str
    model_revision: str
    backend: Backend
    dtype: str
    owner: OwnerRef | None = None


@dataclass(frozen=True)
class Capabilities:
    can_load: bool = False
    can_unload: bool = False
    can_sleep_l1: bool = False
    can_sleep_l2: bool = False
    can_wake: bool = False
    can_observe_health: bool = False
    evidence: CapabilityEvidence = CapabilityEvidence.UNAVAILABLE

    def supports(self, action: Action) -> bool:
        if action is Action.LOAD:
            return self.can_load
        if action is Action.UNLOAD:
            return self.can_unload
        if action is Action.SLEEP:
            return self.can_sleep_l1 or self.can_sleep_l2
        if action is Action.WAKE:
            return self.can_wake
        raise IllegalTransitionError(f"unknown action {action!r}")


@dataclass(frozen=True)
class ServiceRecord:
    identity: ServiceIdentity
    capabilities: Capabilities
    state: ServiceState
    configured_reservation_mib: int
    measured_resident_mib: int | None = None
    measurement_generation: int = 0
    measurement_monotonic_s: float | None = None

    @property
    def name(self) -> ServiceName:
        return self.identity.name


@dataclass(frozen=True)
class Registry:
    """Immutable-by-record registry with a monotone generation (spec §11)."""

    records: tuple[ServiceRecord, ...]
    generation: int = 0

    def by_name(self) -> dict[ServiceName, ServiceRecord]:
        mapping: dict[ServiceName, ServiceRecord] = {}
        for record in self.records:
            if record.name in mapping:
                raise AmbiguousOwnershipError(
                    f"duplicate registry entry for {record.name.value}"
                )
            mapping[record.name] = record
        return mapping

    def get(self, name: ServiceName) -> ServiceRecord:
        record = self.by_name().get(name)
        if record is None:
            raise ServiceUnavailableError(f"unknown service {name.value}")
        return record

    def with_record(self, record: ServiceRecord) -> "Registry":
        replaced = tuple(
            record if existing.name == record.name else existing
            for existing in self.records
        )
        return Registry(records=replaced, generation=self.generation + 1)


@dataclass(frozen=True)
class ProfileMember:
    service: ServiceName
    required: bool


@dataclass(frozen=True)
class ProfileDefinition:
    profile: ResidencyProfile
    members: tuple[ProfileMember, ...]
    exclusive: bool = False
    #: Services that must NOT be resident while this profile is active
    #: (spec §14 — the negative constraint of ``P-Verify-Hard``).
    excluded: tuple[ServiceName, ...] = ()


@dataclass(frozen=True)
class ProfileVerdict:
    profile: ResidencyProfile
    availability: ProfileAvailability
    reason: ReasonCode
    missing_required: tuple[ServiceName, ...] = ()
    missing_optional: tuple[ServiceName, ...] = ()
    missing_capabilities: tuple[tuple[ServiceName, Action], ...] = ()

    @property
    def is_available(self) -> bool:
        return self.availability is ProfileAvailability.AVAILABLE


@dataclass(frozen=True)
class PlanStep:
    service: ServiceName
    action: Action
    #: Total accounted MiB **after** this step — the intermediate state the
    #: invariant is checked against (spec §13.8).
    accounted_after_mib: int


@dataclass(frozen=True)
class TransitionPlan:
    target: ResidencyProfile
    steps: tuple[PlanStep, ...]
    rollback: tuple[PlanStep, ...]
    generation: int
    budget_mib: int
    accounted_before_mib: int

    @property
    def is_noop(self) -> bool:
        return not self.steps


# --------------------------------------------------------------------------- #
# Profile catalog (spec §14)
# --------------------------------------------------------------------------- #
def _member(service: ServiceName, required: bool = True) -> ProfileMember:
    return ProfileMember(service=service, required=required)


PROFILE_CATALOG: Mapping[ResidencyProfile, ProfileDefinition] = {
    ResidencyProfile.P_ONLINE_TEXT: ProfileDefinition(
        profile=ResidencyProfile.P_ONLINE_TEXT,
        members=(
            _member(ServiceName.EMB_QWEN3_8B),
            _member(ServiceName.RERANK_QWEN3_8B),
        ),
    ),
    ResidencyProfile.P_ONLINE_MM: ProfileDefinition(
        profile=ResidencyProfile.P_ONLINE_MM,
        members=(
            _member(ServiceName.EMB_QWEN3_8B),
            _member(ServiceName.RERANK_QWEN3_8B),
            _member(ServiceName.JINA_CLIP),
            _member(ServiceName.OCR),
            _member(ServiceName.VLM_8B),
        ),
    ),
    ResidencyProfile.P_VERIFY_HARD: ProfileDefinition(
        profile=ResidencyProfile.P_VERIFY_HARD,
        members=(_member(ServiceName.VLM_32B),),
        exclusive=True,
        excluded=(ServiceName.EMB_QWEN3_8B, ServiceName.RERANK_QWEN3_8B),
    ),
    ResidencyProfile.P_INGEST_DOCS: ProfileDefinition(
        profile=ResidencyProfile.P_INGEST_DOCS,
        members=(
            _member(ServiceName.OCR),
            _member(ServiceName.DOCLING, required=False),
            _member(ServiceName.EMB_QWEN3_8B),
        ),
    ),
    ResidencyProfile.P_INGEST_VISUAL: ProfileDefinition(
        profile=ResidencyProfile.P_INGEST_VISUAL,
        members=(
            _member(ServiceName.JINA_CLIP),
            _member(ServiceName.COLQWEN),
            _member(ServiceName.EMB_QWEN3_8B),
        ),
    ),
}


# --------------------------------------------------------------------------- #
# Pure route → profile mapping (spec §9)
# --------------------------------------------------------------------------- #
#: HBIM-073 §34/§36 — the services a route actually dispatches. This is
#: deliberately narrower than the residency *profile*: a profile describes what
#: may be co-resident, while this set describes what a request truly needs. The
#: document route serves BM25 + dense + RRF and never calls the reranker, so a
#: missing reranker must not block it — and a missing embedding service must.
_ROUTE_REQUIRED_SERVICES: Mapping[str, tuple[ServiceName, ...]] = MappingProxyType(
    {
        "hybrid_semantic": (ServiceName.EMB_QWEN3_8B, ServiceName.RERANK_QWEN3_8B),
        "document_hybrid": (ServiceName.EMB_QWEN3_8B,),
        "multimodal": (
            ServiceName.EMB_QWEN3_8B,
            ServiceName.RERANK_QWEN3_8B,
            ServiceName.JINA_CLIP,
            ServiceName.OCR,
            ServiceName.VLM_8B,
        ),
    }
)


def required_services_for_route(route: "Route", *, degraded: bool) -> tuple[ServiceName, ...]:
    """The exact services a route dispatches; ``()`` when it needs none.

    Availability for the document route is decided from this set, never from
    the wider profile membership, so "reranker absent" cannot block a route
    that provably never calls it.
    """
    if not isinstance(degraded, bool):
        raise IllegalTransitionError("degraded must be a bool")
    if degraded:
        return ()
    profile = profile_for_route(route, degraded=False)
    if profile is None:
        return ()
    return _ROUTE_REQUIRED_SERVICES.get(route.value, ())


def profile_for_route(route: "Route", *, degraded: bool) -> ResidencyProfile | None:
    """Pure, total, exhaustive map from a deterministic route to a profile.

    ``None`` is the **typed** "no GPU residency required" outcome, not a silent
    default: the routes that return it dispatch no model service (spec §9.5).
    A degraded route always returns ``None`` because the endpoint already falls
    through to the legacy, model-free path.

    This function performs no I/O and imports nothing from the router beyond
    the ``Route`` enum, so ``retrieval.router`` stays pure and untouched.
    """
    from retrieval.router import Route as _Route

    if not isinstance(degraded, bool):
        raise IllegalTransitionError("degraded must be a bool")
    if degraded:
        return None
    if route is _Route.HYBRID_SEMANTIC:
        return ResidencyProfile.P_ONLINE_TEXT
    if route is _Route.MULTIMODAL:
        return ResidencyProfile.P_ONLINE_MM
    if route is _Route.DOCUMENT_HYBRID:
        # HBIM-073 §36 / §4 C-2 — textual chunk retrieval needs no visual
        # service. Keeping P_ONLINE_MM would make the route unavailable
        # whenever Jina CLIP, OCR or the VLM are absent, which is the normal
        # state. The *exact* services this route dispatches are narrower still
        # (``required_services_for_route``): under the reviewed
        # ``disabled_rrf_only`` decision the reranker is never called.
        return ResidencyProfile.P_ONLINE_TEXT
    if route in (
        _Route.EXACT_LOOKUP,
        _Route.AGGREGATION,
        _Route.STRUCTURED,
        _Route.GRAPH,
        _Route.CHAT,
    ):
        return None
    raise IllegalTransitionError(f"route {route!r} has no residency mapping")


# --------------------------------------------------------------------------- #
# VRAM accounting (spec §13)
# --------------------------------------------------------------------------- #
def validate_mib(value: object, field: str) -> int:
    """Reject bool, non-integral, negative, NaN and infinite MiB values."""
    if isinstance(value, bool):
        raise ValueError(f"{field} must be an integer MiB value, not a bool")
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{field} must be finite")
        if not value.is_integer():
            raise ValueError(f"{field} must be an integral MiB value")
        value = int(value)
    if not isinstance(value, int):
        raise ValueError(f"{field} must be an integer MiB value")
    if value < 0:
        raise ValueError(f"{field} must not be negative")
    return value


def derive_budget_mib(
    *, total_mib: int, reserve_mib: int, explicit_budget_mib: int | None = None
) -> int:
    """Explicit budget when set, else ``total − reserve`` (spec §10)."""
    if explicit_budget_mib is not None:
        budget = validate_mib(explicit_budget_mib, "vram_budget_mib")
        if budget <= 0:
            raise ValueError("vram_budget_mib must be positive")
        return budget
    total = validate_mib(total_mib, "vram_total_mib")
    reserve = validate_mib(reserve_mib, "vram_reserve_mib")
    if total <= reserve:
        raise ValueError("vram_total_mib must exceed vram_reserve_mib")
    return total - reserve


def effective_accounted_mib(record: ServiceRecord) -> int:
    """Conservative per-service accounting: ``max(configured, measured or 0)``.

    ``measured_resident_mib is None`` means *unmeasurable* (spec §2/§13.1); it
    is never treated as zero-usage, because the configured reservation always
    dominates the maximum.
    """
    configured = validate_mib(
        record.configured_reservation_mib, "configured_reservation_mib"
    )
    measured = record.measured_resident_mib
    if measured is None:
        return configured
    return max(configured, validate_mib(measured, "measured_resident_mib"))


#: States that hold VRAM. ``loading``/``waking``/``unloading`` are accounted at
#: the full reservation so the invariant covers the load peak (spec §12/§13.8);
#: ``failed`` is conservative — the service may still be holding memory.
_RESIDENT_STATES = frozenset(
    {
        ServiceState.LOADING,
        ServiceState.LOADED,
        ServiceState.WAKING,
        ServiceState.UNLOADING,
        ServiceState.FAILED,
    }
)


def state_accounted_mib(record: ServiceRecord) -> int:
    """Accounted MiB for a record in its current state."""
    if record.state in _RESIDENT_STATES:
        return effective_accounted_mib(record)
    return 0


def accounted_total_mib(registry: Registry) -> int:
    return sum(state_accounted_mib(record) for record in registry.records)


# --------------------------------------------------------------------------- #
# Profile availability (spec §15)
# --------------------------------------------------------------------------- #
def _actions_needed(
    definition: ProfileDefinition, records: Mapping[ServiceName, ServiceRecord]
) -> list[tuple[ServiceName, Action]]:
    """Capabilities the profile would require from *present* services."""
    needed: list[tuple[ServiceName, Action]] = []
    for member in definition.members:
        record = records.get(member.service)
        if record is None or record.state is ServiceState.UNAVAILABLE:
            continue
        if record.state in (ServiceState.UNLOADED, ServiceState.FAILED):
            needed.append((member.service, Action.LOAD))
        elif record.state is ServiceState.SLEEPING:
            needed.append((member.service, Action.WAKE))
    for name in definition.excluded:
        record = records.get(name)
        if record is None or record.state is ServiceState.UNAVAILABLE:
            continue
        if record.state is ServiceState.LOADED:
            # Either mechanism satisfies the negative constraint.
            if not (
                record.capabilities.supports(Action.SLEEP)
                or record.capabilities.supports(Action.UNLOAD)
            ):
                needed.append((name, Action.SLEEP))
    return needed


def evaluate_profile(registry: Registry, profile: ResidencyProfile) -> ProfileVerdict:
    """Typed availability with the deterministic precedence of spec §15."""
    definition = PROFILE_CATALOG.get(profile)
    if definition is None:
        raise ProfileUnavailableError(f"unknown profile {profile!r}")
    records = registry.by_name()

    missing_required: list[ServiceName] = []
    missing_optional: list[ServiceName] = []
    for member in definition.members:
        record = records.get(member.service)
        absent = record is None or record.state is ServiceState.UNAVAILABLE
        if not absent:
            continue
        if member.required:
            missing_required.append(member.service)
        else:
            missing_optional.append(member.service)

    missing_caps: list[tuple[ServiceName, Action]] = [
        (name, action)
        for name, action in _actions_needed(definition, records)
        if not records[name].capabilities.supports(action)
    ]

    # Rule 1 wins, but the capability block is still recorded (spec §15).
    if missing_required:
        return ProfileVerdict(
            profile=profile,
            availability=ProfileAvailability.UNAVAILABLE,
            reason=ReasonCode.MISSING_REQUIRED_MEMBER,
            missing_required=tuple(missing_required),
            missing_optional=tuple(missing_optional),
            missing_capabilities=tuple(missing_caps),
        )
    if missing_caps:
        return ProfileVerdict(
            profile=profile,
            availability=ProfileAvailability.BLOCKED_BY_CAPABILITY,
            reason=ReasonCode.MISSING_CAPABILITY,
            missing_optional=tuple(missing_optional),
            missing_capabilities=tuple(missing_caps),
        )
    if missing_optional:
        return ProfileVerdict(
            profile=profile,
            availability=ProfileAvailability.DEGRADED,
            reason=ReasonCode.MISSING_OPTIONAL_MEMBER,
            missing_optional=tuple(missing_optional),
        )
    return ProfileVerdict(
        profile=profile,
        availability=ProfileAvailability.AVAILABLE,
        reason=ReasonCode.OK,
    )


# --------------------------------------------------------------------------- #
# Pure transition planner (spec §16)
# --------------------------------------------------------------------------- #
_INVERSE: Mapping[Action, Action] = {
    Action.LOAD: Action.UNLOAD,
    Action.UNLOAD: Action.LOAD,
    Action.SLEEP: Action.WAKE,
    Action.WAKE: Action.SLEEP,
}


def _release_action(record: ServiceRecord) -> Action | None:
    """Preferred release for a resident non-member: sleep, else unload."""
    if record.capabilities.supports(Action.SLEEP):
        return Action.SLEEP
    if record.capabilities.supports(Action.UNLOAD):
        return Action.UNLOAD
    return None


def _acquire_action(record: ServiceRecord) -> Action | None:
    if record.state is ServiceState.SLEEPING:
        return Action.WAKE
    if record.state in (ServiceState.UNLOADED, ServiceState.FAILED):
        return Action.LOAD
    return None


def plan_transition(
    registry: Registry, target: ResidencyProfile, budget_mib: int
) -> TransitionPlan:
    """Pure planner: no I/O, no clock, no randomness, no logging.

    Release-before-acquire, capacity reserved before every acquire, the budget
    invariant checked at **every intermediate state**, and a deterministic
    rollback computed at plan time. Every refusal happens before any effect.
    """
    budget = validate_mib(budget_mib, "budget_mib")
    definition = PROFILE_CATALOG.get(target)
    if definition is None:
        raise ProfileUnavailableError(f"unknown profile {target!r}")

    verdict = evaluate_profile(registry, target)
    if verdict.availability is ProfileAvailability.UNAVAILABLE:
        raise ProfileUnavailableError(
            f"{target.value} missing required members: "
            + ",".join(name.value for name in verdict.missing_required)
        )
    if verdict.availability is ProfileAvailability.BLOCKED_BY_CAPABILITY:
        raise CapabilityUnavailableError(
            f"{target.value} needs unsupported actions: "
            + ",".join(f"{n.value}:{a.value}" for n, a in verdict.missing_capabilities)
        )

    records = registry.by_name()
    member_names = {member.service for member in definition.members}
    # Services that must be released: explicit exclusions plus any resident
    # service that is not a member of the target profile.
    release_names = set(definition.excluded)
    for name, record in records.items():
        if name in member_names:
            continue
        if record.state in (ServiceState.LOADED, ServiceState.FAILED):
            release_names.add(name)

    working: dict[ServiceName, ServiceRecord] = dict(records)

    def total() -> int:
        return sum(state_accounted_mib(record) for record in working.values())

    accounted_before = total()
    steps: list[PlanStep] = []

    # --- release phase: descending accounted, then name (deterministic) ---
    releasable = [
        (name, working[name])
        for name in sorted(release_names, key=lambda item: item.value)
        if name in working
        and working[name].state in (ServiceState.LOADED, ServiceState.FAILED)
    ]
    releasable.sort(key=lambda pair: (-state_accounted_mib(pair[1]), pair[0].value))
    for name, record in releasable:
        action = _release_action(record)
        if action is None:
            # A service we cannot release is only fatal when the target
            # profile structurally forbids its residency (spec §14).
            if name in definition.excluded:
                raise CapabilityUnavailableError(
                    f"{name.value} cannot be released for {target.value}"
                )
            continue
        new_state = (
            ServiceState.SLEEPING if action is Action.SLEEP else ServiceState.UNLOADED
        )
        working[name] = replace(record, state=new_state)
        steps.append(
            PlanStep(service=name, action=action, accounted_after_mib=total())
        )

    # --- acquire phase: ascending reservation, then name ---
    acquirable = [
        (member.service, working[member.service])
        for member in definition.members
        if member.service in working
        and working[member.service].state is not ServiceState.UNAVAILABLE
    ]
    acquirable.sort(key=lambda pair: (pair[1].configured_reservation_mib, pair[0].value))
    for name, record in acquirable:
        action = _acquire_action(record)
        if action is None:
            continue
        if not record.capabilities.supports(action):
            raise CapabilityUnavailableError(
                f"{name.value} does not support {action.value}"
            )
        working[name] = replace(record, state=ServiceState.LOADED)
        after = total()
        if after > budget:
            raise OverBudgetError(
                f"{target.value} would need {after} MiB, budget {budget} MiB"
            )
        steps.append(PlanStep(service=name, action=action, accounted_after_mib=after))

    # Final-state invariant (also covers a pure-release plan).
    final_total = total()
    if final_total > budget:
        raise OverBudgetError(
            f"{target.value} would need {final_total} MiB, budget {budget} MiB"
        )

    # --- deterministic rollback, refused when any action is irreversible ---
    rollback: list[PlanStep] = []
    for step in reversed(steps):
        inverse = _INVERSE[step.action]
        if not records[step.service].capabilities.supports(inverse):
            raise IrreversiblePlanError(
                f"{step.service.value} cannot undo {step.action.value}"
            )
        rollback.append(
            PlanStep(
                service=step.service,
                action=inverse,
                accounted_after_mib=step.accounted_after_mib,
            )
        )

    return TransitionPlan(
        target=target,
        steps=tuple(steps),
        rollback=tuple(rollback),
        generation=registry.generation,
        budget_mib=budget,
        accounted_before_mib=accounted_before,
    )


# --------------------------------------------------------------------------- #
# Declarative catalog of the current and future slots (spec §7, §8)
# --------------------------------------------------------------------------- #
#: Observe-only capabilities: the pinned TEI and vLLM deployments expose no
#: load/unload/sleep/wake route (spec §7, re-proven live by the §31 suite).
_OBSERVE_ONLY = Capabilities(
    can_observe_health=True, evidence=CapabilityEvidence.PROVEN_LIVE
)
_NO_CAPABILITY = Capabilities(evidence=CapabilityEvidence.UNAVAILABLE)


def default_registry(
    *,
    emb_reservation_mib: int,
    rerank_reservation_mib: int,
    emb_state: ServiceState = ServiceState.LOADED,
    rerank_state: ServiceState = ServiceState.LOADED,
) -> Registry:
    """The merged deployment: two present observe-only services, six declared
    future slots that are ``unavailable`` and can never become ``loaded``."""

    def future(name: ServiceName, reservation_mib: int) -> ServiceRecord:
        return ServiceRecord(
            identity=ServiceIdentity(
                name=name,
                model_id="",
                model_revision="",
                backend=Backend.NONE,
                dtype="",
                owner=None,
            ),
            capabilities=_NO_CAPABILITY,
            state=ServiceState.UNAVAILABLE,
            configured_reservation_mib=reservation_mib,
        )

    records = (
        ServiceRecord(
            identity=ServiceIdentity(
                name=ServiceName.EMB_QWEN3_8B,
                model_id="Qwen/Qwen3-Embedding-8B",
                model_revision="1d8ad4ca9b3dd8059ad90a75d4983776a23d44af",
                backend=Backend.TEI,
                dtype="float16",
                owner=OwnerRef("hbim-rag", "embeddings", "HBIM-030"),
            ),
            capabilities=_OBSERVE_ONLY,
            state=emb_state,
            configured_reservation_mib=validate_mib(
                emb_reservation_mib, "emb_reservation_mib"
            ),
        ),
        ServiceRecord(
            identity=ServiceIdentity(
                name=ServiceName.RERANK_QWEN3_8B,
                model_id="Qwen/Qwen3-Reranker-8B",
                model_revision="77d193c791ed757ca307ee72715aa132723da912",
                backend=Backend.VLLM,
                dtype="bfloat16",
                owner=OwnerRef("hbim-rag", "reranker", "HBIM-051"),
            ),
            capabilities=_OBSERVE_ONLY,
            state=rerank_state,
            configured_reservation_mib=validate_mib(
                rerank_reservation_mib, "rerank_reservation_mib"
            ),
        ),
        # Declared future slots (spec §8): no endpoint, image or weight.
        future(ServiceName.JINA_CLIP, 3072),
        future(ServiceName.OCR, 5120),
        future(ServiceName.DOCLING, 2048),
        future(ServiceName.VLM_8B, 10240),
        future(ServiceName.VLM_32B, 38912),
        future(ServiceName.COLQWEN, 8192),
    )
    return Registry(records=records, generation=0)


def owned_service_names() -> tuple[ServiceName, ...]:
    """The closed control allowlist (spec §24)."""
    return (ServiceName.EMB_QWEN3_8B, ServiceName.RERANK_QWEN3_8B)


def resolve_owner(
    records: Sequence[ServiceRecord], owner: OwnerRef
) -> ServiceRecord:
    """Exact-match ownership resolution — never substring, prefix or regex."""
    matches = [
        record
        for record in records
        if record.identity.owner is not None and record.identity.owner == owner
    ]
    if not matches:
        raise ServiceUnavailableError("ownership_unverified")
    if len(matches) > 1:
        raise AmbiguousOwnershipError("ambiguous_ownership")
    return matches[0]


# --------------------------------------------------------------------------- #
# Transition results and status (spec §17, §25, §28)
# --------------------------------------------------------------------------- #
class TransitionOutcome(str, Enum):
    NOOP = "noop"
    APPLIED = "applied"
    UNAVAILABLE = "unavailable"
    BLOCKED = "blocked"
    FAILED = "failed"


@dataclass(frozen=True)
class TransitionResult:
    target: ResidencyProfile
    outcome: TransitionOutcome
    reason: ReasonCode
    transition_id: str
    executed: tuple[PlanStep, ...] = ()
    rolled_back: tuple[PlanStep, ...] = ()
    verdict: ProfileVerdict | None = None
    accounted_mib: int = 0
    budget_mib: int = 0

    @property
    def ok(self) -> bool:
        return self.outcome in (TransitionOutcome.NOOP, TransitionOutcome.APPLIED)


class TransitionFailedError(ResidencyError):
    reason = ReasonCode.ACTION_FAILED

    def __init__(self, message: str, result: TransitionResult) -> None:
        super().__init__(message)
        self.result = result


class RollbackFailedError(ResidencyError):
    reason = ReasonCode.ROLLBACK_FAILED

    def __init__(self, message: str, result: TransitionResult) -> None:
        super().__init__(message)
        self.result = result


class RestorationFailedError(ResidencyError):
    reason = ReasonCode.RESTORATION_FAILED


class ReentrantTransitionError(ResidencyError):
    reason = ReasonCode.REENTRANT


@dataclass(frozen=True)
class ResidencyStatus:
    """Sanitised status (spec §25/§26): closed enums and integers only."""

    active_profile: ResidencyProfile | None
    generation: int
    accounted_mib: int
    budget_mib: int
    services: tuple[dict[str, object], ...]
    profiles: tuple[dict[str, object], ...]
    #: Whole-GPU sample minus the accounted total. ``None`` means the sample is
    #: unavailable — explicitly unknown, never a comfortable 0 (spec §13.6).
    reconciliation_drift_mib: int | None = None
    reconciliation_reason: ReasonCode = ReasonCode.OK


class ResidencyManager:
    """Capability-gated residency control (spec §17, §19, §20, §21, §22).

    Effectful, but every effect goes through an injected adapter. The async
    mutex is created **lazily** so importing this module never binds a lock to
    an event loop (spec §27).
    """

    def __init__(
        self,
        registry: Registry,
        adapters: Mapping[ServiceName, object],
        *,
        budget_mib: int,
        action_timeout_s: float = 60.0,
        transition_timeout_s: float = 120.0,
        lock_timeout_s: float = 300.0,
        active_profile: ResidencyProfile | None = None,
        reconciliation_tolerance_mib: int = 512,
        gpu_used_probe: object | None = None,
    ) -> None:
        self._registry = registry
        self._adapters = dict(adapters)
        self._budget_mib = validate_mib(budget_mib, "budget_mib")
        self._action_timeout_s = action_timeout_s
        self._transition_timeout_s = transition_timeout_s
        self._lock_timeout_s = lock_timeout_s
        self._active_profile = active_profile
        self._reconciliation_tolerance_mib = validate_mib(
            reconciliation_tolerance_mib, "reconciliation_tolerance_mib"
        )
        #: Optional whole-GPU sampler. It is NEVER attributed to one service
        #: (spec §13.3); it only detects drift between what the registry
        #: accounts for and what the device actually reports.
        self._gpu_used_probe = gpu_used_probe
        self._drift_mib: int | None = None
        self._drift_reason = ReasonCode.OK
        self._lock: object | None = None          # lazily created asyncio.Lock
        self._exclusive: object | None = None     # lazily created asyncio.Lock
        self._inflight: dict[ResidencyProfile, "asyncio.Future[TransitionResult]"] = {}
        self._transition_depth = 0
        self._transition_counter = 0

    # -- introspection -------------------------------------------------- #
    @property
    def registry(self) -> Registry:
        return self._registry

    @property
    def active_profile(self) -> ResidencyProfile | None:
        return self._active_profile

    @property
    def budget_mib(self) -> int:
        return self._budget_mib

    def _next_transition_id(self) -> str:
        """Opaque, monotone, carries no host, user or process identifier."""
        self._transition_counter += 1
        return f"t{self._transition_counter:06d}"

    def _mutation_lock(self):  # type: ignore[no-untyped-def]
        import asyncio

        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    def _exclusive_lock(self):  # type: ignore[no-untyped-def]
        import asyncio

        if self._exclusive is None:
            self._exclusive = asyncio.Lock()
        return self._exclusive

    def status(self) -> ResidencyStatus:
        """Read-only projection. Mutates nothing — not even measurements."""
        services: tuple[dict[str, object], ...] = tuple(
            {
                "service": record.name.value,
                "state": record.state.value,
                "backend": record.identity.backend.value,
                "configured_reservation_mib": record.configured_reservation_mib,
                # Unmeasurable is explicit, never 0 (spec §13.1).
                "measured_resident_mib": (
                    "unavailable"
                    if record.measured_resident_mib is None
                    else record.measured_resident_mib
                ),
                "effective_accounted_mib": state_accounted_mib(record),
                "can_load": record.capabilities.can_load,
                "can_unload": record.capabilities.can_unload,
                "can_sleep": record.capabilities.supports(Action.SLEEP),
                "can_wake": record.capabilities.can_wake,
                "can_observe_health": record.capabilities.can_observe_health,
                "capability_evidence": record.capabilities.evidence.value,
            }
            for record in sorted(self._registry.records, key=lambda r: r.name.value)
        )
        profiles: list[dict[str, object]] = []
        for profile in ResidencyProfile:
            verdict = evaluate_profile(self._registry, profile)
            profiles.append(
                {
                    "profile": profile.value,
                    "availability": verdict.availability.value,
                    "reason": verdict.reason.value,
                    "missing_required": [n.value for n in verdict.missing_required],
                    "missing_optional": [n.value for n in verdict.missing_optional],
                    "missing_capabilities": [
                        f"{n.value}:{a.value}" for n, a in verdict.missing_capabilities
                    ],
                }
            )
        return ResidencyStatus(
            active_profile=self._active_profile,
            generation=self._registry.generation,
            accounted_mib=accounted_total_mib(self._registry),
            budget_mib=self._budget_mib,
            services=services,
            profiles=tuple(profiles),
            reconciliation_drift_mib=self._drift_mib,
            reconciliation_reason=self._drift_reason,
        )

    # -- reconciliation --------------------------------------------------- #
    async def _observe(self, name: ServiceName) -> ServiceState:
        """Health **and** identity agreement, else not loaded (spec §12)."""
        record = self._registry.get(name)
        adapter = self._adapters.get(name)
        if adapter is None or record.state is ServiceState.UNAVAILABLE:
            return ServiceState.UNAVAILABLE
        try:
            healthy = await adapter.health()  # type: ignore[attr-defined]
        except Exception:
            return ServiceState.FAILED
        if not healthy:
            return ServiceState.FAILED
        try:
            snapshot = await adapter.identity()  # type: ignore[attr-defined]
        except Exception:
            return ServiceState.FAILED
        if snapshot.model_id != record.identity.model_id:
            return ServiceState.FAILED
        if (
            record.identity.model_revision
            and snapshot.model_revision
            and snapshot.model_revision != record.identity.model_revision
        ):
            return ServiceState.FAILED
        return ServiceState.LOADED

    async def reconcile(self) -> ResidencyStatus:
        """Re-observe reality. Corrects records (§12), executes no action."""
        registry = self._registry
        for record in registry.records:
            if record.state is ServiceState.UNAVAILABLE:
                continue
            observed = await self._observe(record.name)
            if observed is not record.state:
                registry = registry.with_record(
                    replace(registry.get(record.name), state=observed)
                )
        self._registry = registry
        self._reconcile_drift()
        return self.status()

    def _reconcile_drift(self) -> None:
        """§13.6 — compare the whole-GPU sample with the accounted total.

        Drift beyond the tolerance is REPORTED, never silently absorbed and
        never attributed to a single service. Without a sampler the drift is
        explicitly unknown (``None``).
        """
        probe = self._gpu_used_probe
        if probe is None:
            self._drift_mib = None
            self._drift_reason = ReasonCode.OK
            return
        try:
            sample = probe()  # type: ignore[operator]
        except Exception:
            self._drift_mib = None
            self._drift_reason = ReasonCode.OK
            return
        if sample is None:
            self._drift_mib = None
            self._drift_reason = ReasonCode.OK
            return
        used = validate_mib(sample, "gpu_used_mib")
        self._drift_mib = used - accounted_total_mib(self._registry)
        self._drift_reason = (
            ReasonCode.RECONCILIATION_DRIFT
            if abs(self._drift_mib) > self._reconciliation_tolerance_mib
            else ReasonCode.OK
        )

    # -- execution -------------------------------------------------------- #
    async def _apply_step(self, step: PlanStep) -> None:
        import asyncio

        if step.service not in owned_service_names() and step.service in (
            ServiceName.EMB_QWEN3_8B,
            ServiceName.RERANK_QWEN3_8B,
        ):  # pragma: no cover - defensive; the allowlist is closed
            raise ServiceUnavailableError("ownership_unverified")
        adapter = self._adapters.get(step.service)
        if adapter is None:
            raise ServiceUnavailableError(f"{step.service.value} has no adapter")
        record = self._registry.get(step.service)
        if not record.capabilities.supports(step.action):
            raise CapabilityUnavailableError(
                f"{step.service.value} does not support {step.action.value}"
            )
        await asyncio.wait_for(
            adapter.apply(step.action),  # type: ignore[attr-defined]
            timeout=self._action_timeout_s,
        )

    def _state_after(self, action: Action) -> ServiceState:
        if action is Action.LOAD or action is Action.WAKE:
            return ServiceState.LOADED
        if action is Action.SLEEP:
            return ServiceState.SLEEPING
        return ServiceState.UNLOADED

    async def _execute(self, plan: TransitionPlan, transition_id: str) -> TransitionResult:
        if plan.generation != self._registry.generation:
            raise StalePlanError("plan generation does not match the registry")
        executed: list[PlanStep] = []
        try:
            for step in plan.steps:
                await self._apply_step(step)
                self._registry = self._registry.with_record(
                    replace(
                        self._registry.get(step.service),
                        state=self._state_after(step.action),
                    )
                )
                executed.append(step)
        except BaseException as exc:  # includes CancelledError (spec §22)
            self._registry = self._registry.with_record(
                replace(
                    self._registry.get(executed[-1].service)
                    if executed
                    else self._registry.get(plan.steps[0].service),
                    state=ServiceState.FAILED,
                )
            )
            rolled_back = await self._rollback(executed, transition_id, plan)
            result = TransitionResult(
                target=plan.target,
                outcome=TransitionOutcome.FAILED,
                reason=(
                    ReasonCode.CANCELLED
                    if isinstance(exc, BaseException)
                    and type(exc).__name__ == "CancelledError"
                    else ReasonCode.ACTION_FAILED
                ),
                transition_id=transition_id,
                executed=tuple(executed),
                rolled_back=rolled_back,
                accounted_mib=accounted_total_mib(self._registry),
                budget_mib=self._budget_mib,
            )
            if isinstance(exc, Exception):
                raise TransitionFailedError(str(exc), result) from exc
            raise
        return TransitionResult(
            target=plan.target,
            outcome=TransitionOutcome.APPLIED,
            reason=ReasonCode.OK,
            transition_id=transition_id,
            executed=tuple(executed),
            accounted_mib=accounted_total_mib(self._registry),
            budget_mib=self._budget_mib,
        )

    async def _rollback(
        self, executed: Sequence[PlanStep], transition_id: str, plan: TransitionPlan
    ) -> tuple[PlanStep, ...]:
        """Inverse actions, reverse order. Failure is never swallowed."""
        undone: list[PlanStep] = []
        for step in reversed(list(executed)):
            inverse = PlanStep(
                service=step.service,
                action=_INVERSE[step.action],
                accounted_after_mib=step.accounted_after_mib,
            )
            try:
                await self._apply_step(inverse)
            except Exception as exc:
                result = TransitionResult(
                    target=plan.target,
                    outcome=TransitionOutcome.FAILED,
                    reason=ReasonCode.ROLLBACK_FAILED,
                    transition_id=transition_id,
                    executed=tuple(executed),
                    rolled_back=tuple(undone),
                    accounted_mib=accounted_total_mib(self._registry),
                    budget_mib=self._budget_mib,
                )
                raise RollbackFailedError(str(exc), result) from exc
            self._registry = self._registry.with_record(
                replace(
                    self._registry.get(step.service),
                    state=self._state_after(inverse.action),
                )
            )
            undone.append(inverse)
        return tuple(undone)

    # -- public API ------------------------------------------------------- #
    async def ensure_profile(self, target: ResidencyProfile) -> TransitionResult:
        """Idempotent, coalescing, serialised, fail-closed (spec §17/§19/§20)."""
        import asyncio

        if not isinstance(target, ResidencyProfile):
            raise ProfileUnavailableError(f"unknown profile {target!r}")
        if self._transition_depth > 0:
            raise ReentrantTransitionError("nested ensure_profile is forbidden")

        inflight = self._inflight.get(target)
        if inflight is not None:
            return await asyncio.shield(inflight)

        task = asyncio.ensure_future(self._ensure_locked(target))
        self._inflight[target] = task
        try:
            return await task
        finally:
            self._inflight.pop(target, None)

    async def _ensure_locked(self, target: ResidencyProfile) -> TransitionResult:
        import asyncio

        definition = PROFILE_CATALOG[target]
        transition_id = self._next_transition_id()
        try:
            await asyncio.wait_for(
                self._mutation_lock().acquire(), timeout=self._lock_timeout_s
            )
        except asyncio.TimeoutError as exc:
            raise TransitionFailedError(
                "mutation lock timeout",
                TransitionResult(
                    target=target,
                    outcome=TransitionOutcome.FAILED,
                    reason=ReasonCode.ACTION_TIMEOUT,
                    transition_id=transition_id,
                    budget_mib=self._budget_mib,
                ),
            ) from exc

        exclusive_held = False
        previous_profile = self._active_profile
        self._transition_depth += 1
        try:
            if definition.exclusive:
                await asyncio.wait_for(
                    self._exclusive_lock().acquire(), timeout=self._lock_timeout_s
                )
                exclusive_held = True

            verdict = evaluate_profile(self._registry, target)
            if verdict.availability in (
                ProfileAvailability.UNAVAILABLE,
                ProfileAvailability.BLOCKED_BY_CAPABILITY,
            ):
                return TransitionResult(
                    target=target,
                    outcome=(
                        TransitionOutcome.UNAVAILABLE
                        if verdict.availability is ProfileAvailability.UNAVAILABLE
                        else TransitionOutcome.BLOCKED
                    ),
                    reason=verdict.reason,
                    transition_id=transition_id,
                    verdict=verdict,
                    accounted_mib=accounted_total_mib(self._registry),
                    budget_mib=self._budget_mib,
                )

            plan = plan_transition(self._registry, target, self._budget_mib)
            if plan.is_noop:
                self._active_profile = target
                return TransitionResult(
                    target=target,
                    outcome=TransitionOutcome.NOOP,
                    reason=ReasonCode.OK,
                    transition_id=transition_id,
                    verdict=verdict,
                    accounted_mib=accounted_total_mib(self._registry),
                    budget_mib=self._budget_mib,
                )

            result = await asyncio.wait_for(
                self._execute(plan, transition_id), timeout=self._transition_timeout_s
            )
            # Active profile is committed ONLY after a fully successful plan.
            self._active_profile = target
            return replace(result, verdict=verdict)
        except (ProfileUnavailableError, CapabilityUnavailableError) as exc:
            return TransitionResult(
                target=target,
                outcome=(
                    TransitionOutcome.UNAVAILABLE
                    if isinstance(exc, ProfileUnavailableError)
                    else TransitionOutcome.BLOCKED
                ),
                reason=exc.reason,
                transition_id=transition_id,
                accounted_mib=accounted_total_mib(self._registry),
                budget_mib=self._budget_mib,
            )
        finally:
            self._transition_depth -= 1
            if exclusive_held:
                # Restore the captured previous profile, never a constant.
                self._active_profile = previous_profile
                self._exclusive_lock().release()
            self._mutation_lock().release()
