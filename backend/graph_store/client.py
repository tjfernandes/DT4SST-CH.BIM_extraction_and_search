"""HBIM-082 §34–§37 — the project-owned driver wrapper.

A thin facade over the official Neo4j driver. Two things it deliberately does
**not** do: construct anything at import, and let a driver object escape into a
domain API. Everything below the facade is Bolt; everything above it is
project-owned types (§64).

The ``neo4j`` package is imported lazily inside :func:`build_driver`, mirroring
the way ``relations.native_ifc`` defers ``ifcopenshell``. That keeps the unit
lane importable and provably free of any driver work (§34).
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Callable, Iterator, Mapping, Sequence

from shared.config import Neo4jSettings

__all__ = [
    "Neo4jClientError",
    "Neo4jUnavailable",
    "Neo4jDisabled",
    "Neo4jSemanticError",
    "Neo4jHealth",
    "Neo4jDriverHandle",
    "build_driver",
    "TRANSIENT_ERROR_NAMES",
    "MAX_TRANSIENT_ATTEMPTS",
]

#: §36 — bounded retries, deterministic backoff derived from the attempt index
#: (never a clock reading and never a random source).
MAX_TRANSIENT_ATTEMPTS: int = 3
_BACKOFF_BASE_S: float = 0.05

#: §36 — the only failures that may be retried. Matched by class name so this
#: module keeps no import-time dependency on the driver's exception classes.
TRANSIENT_ERROR_NAMES: frozenset[str] = frozenset(
    {"TransientError", "ServiceUnavailable", "SessionExpired", "WriteServiceUnavailable"}
)


class Neo4jClientError(RuntimeError):
    """Base. Messages carry closed codes and identifiers only — never a URI,
    credential, Cypher statement or driver stack detail (§35)."""


class Neo4jDisabled(Neo4jClientError):
    """The deployment has not enabled the graph store."""


class Neo4jUnavailable(Neo4jClientError):
    """The server could not be reached or did not answer in time."""


class Neo4jSemanticError(Neo4jClientError):
    """The server rejected the statement. Never retried (§36)."""


@dataclass(frozen=True)
class Neo4jHealth:
    """§34 — a typed answer. ``health()`` never raises to its caller."""

    reachable: bool
    database: str
    reason: str = ""


def _sanitise(exc: BaseException) -> str:
    """§35 — the class name only.

    A driver message can quote the failing Cypher and, for authentication
    failures, the URI including userinfo. Neither may reach a log or a response,
    so the class name is the whole of what is propagated.
    """
    return type(exc).__name__


def _is_transient(exc: BaseException) -> bool:
    return any(
        cls.__name__ in TRANSIENT_ERROR_NAMES for cls in type(exc).__mro__
    )


#: §34/§35 — the closed set of access modes this facade will forward. Spelled as
#: the driver's own literal values so no import happens at module scope: the
#: driver is loaded lazily in :func:`build_driver`, and `neo4j.READ_ACCESS` and
#: `neo4j.WRITE_ACCESS` are exactly these strings. A membership test against a
#: frozenset keeps an unsupported value — a bool, an object, a typo — out of the
#: driver call entirely.
_ALLOWED_ACCESS_MODES: frozenset[str] = frozenset({"READ", "WRITE"})


@dataclass(frozen=True)
class Neo4jDriverHandle:
    """A closable handle. Domain code receives this, never a driver or session."""

    _driver: Any
    settings: Neo4jSettings

    @contextmanager
    def session(self, *, default_access_mode: str | None = None) -> Iterator[Any]:
        """A session bound to the configured database, always closed.

        ``default_access_mode`` is the **only** driver option this facade
        forwards, and it is drawn from a closed set. Without it the call is
        byte-for-byte the session the writer has always opened; with
        ``READ_ACCESS`` the server itself refuses a write, which is what makes a
        write clause in a serving template a database error rather than a code
        review promise.

        Deliberately not ``**kwargs``: bookmarks, impersonation, routing and the
        database name are not the caller's to choose, and a passthrough would
        make that a matter of discipline instead of type.
        """
        if default_access_mode is None:
            session = self._driver.session(database=self.settings.database)
        else:
            if default_access_mode not in _ALLOWED_ACCESS_MODES:
                # §35 — closed code only; the value is not echoed back because a
                # caller could have passed anything at all.
                raise Neo4jSemanticError(
                    "unsupported Neo4j access mode; expected READ_ACCESS or WRITE_ACCESS"
                )
            session = self._driver.session(
                database=self.settings.database,
                default_access_mode=default_access_mode,
            )
        try:
            yield session
        finally:
            session.close()

    def execute_write(
        self, unit: Callable[..., Any], /, **kwargs: Any
    ) -> Any:
        """§37 — a managed write transaction with an explicit timeout."""
        return self._run(unit, write=True, **kwargs)

    def execute_read(self, unit: Callable[..., Any], /, **kwargs: Any) -> Any:
        """§37 — a managed read transaction with an explicit timeout."""
        return self._run(unit, write=False, **kwargs)

    def _run(self, unit: Callable[..., Any], *, write: bool, **kwargs: Any) -> Any:
        """§37 — an explicit transaction with an explicit timeout.

        Measured: the driver's managed ``execute_read``/``execute_write``
        forward ``**kwargs`` to the *transaction function*, so they cannot
        carry a timeout, and they run their own retry loop on top of ours.
        ``begin_transaction(timeout=...)`` gives the timeout the specification
        requires and leaves retry policy where §36 defines it — here.
        """
        del write  # access mode is not enforced by this deployment's session
        last: BaseException | None = None
        for attempt in range(1, MAX_TRANSIENT_ATTEMPTS + 1):
            try:
                with self.session() as session:
                    tx = session.begin_transaction(
                        timeout=self.settings.transaction_timeout_s
                    )
                    try:
                        result = unit(tx, **kwargs)
                        tx.commit()
                        return result
                    except BaseException:
                        tx.rollback()
                        raise
            except Exception as exc:  # noqa: BLE001 — classified immediately below
                if not _is_transient(exc):
                    # §36 — a verification mismatch, constraint violation or
                    # unknown predicate is a defect, not a blip. Retrying would
                    # either loop or hide it.
                    raise Neo4jSemanticError(_sanitise(exc)) from None
                last = exc
                if attempt < MAX_TRANSIENT_ATTEMPTS:
                    time.sleep(_BACKOFF_BASE_S * attempt)
        raise Neo4jUnavailable(_sanitise(last) if last else "unreachable") from None

    def run_batches(
        self,
        statement: str,
        batches: Sequence[Sequence[Mapping[str, Any]]],
        *,
        parameters: Mapping[str, Any] | None = None,
    ) -> int:
        """Run one static statement over pre-built batches; returns rows written.

        ``statement`` comes from the frozen registry in
        :mod:`graph_store.schema`; only ``$rows`` and ``parameters`` values vary.
        """
        base = dict(parameters or {})
        written = 0
        for rows in batches:
            payload = {**base, "rows": list(rows)}

            def unit(tx: Any, _stmt: str = statement, _payload: dict[str, Any] = payload) -> int:
                result = tx.run(_stmt, **_payload)
                record = result.single()
                return int(record["written"]) if record else 0

            written += int(self.execute_write(unit))
        return written

    def close(self) -> None:
        """Idempotent."""
        try:
            self._driver.close()
        except Exception:  # noqa: BLE001 — closing twice must never raise
            pass


def build_driver(settings: Neo4jSettings) -> Neo4jDriverHandle:
    """§34 — the only place a driver is constructed. Never called at import."""
    if not settings.enabled:
        raise Neo4jDisabled("the graph store is disabled")
    if not settings.uri or not settings.username or settings.password is None:
        raise Neo4jDisabled("the graph store is not fully configured")

    from neo4j import GraphDatabase  # local: importing this module opens nothing

    try:
        driver = GraphDatabase.driver(
            settings.uri,
            auth=(settings.username, settings.password.get_secret_value()),
            max_connection_pool_size=settings.max_pool_size,
            connection_acquisition_timeout=settings.acquisition_timeout_s,
            connection_timeout=settings.connection_timeout_s,
            # A pointer read before the first publication legitimately asks for
            # properties that do not exist yet, and the server answers with one
            # UNRECOGNIZED notification per property. They are expected, they
            # carry the whole query text, and they would flood the log, so the
            # classification is disabled at the source rather than filtered
            # later. Measured: this is the option that works — the driver
            # accepts ``warn_notification_severity`` without complaint and
            # keeps emitting them.
            notifications_disabled_classifications=["UNRECOGNIZED"],
        )
    except Exception as exc:  # noqa: BLE001 — the message could quote the URI
        raise Neo4jUnavailable(_sanitise(exc)) from None
    return Neo4jDriverHandle(_driver=driver, settings=settings)


def health(handle: Neo4jDriverHandle) -> Neo4jHealth:
    """§34 — a typed probe that never raises."""
    try:
        def unit(tx: Any) -> int:
            return int(tx.run("RETURN 1 AS ok").single()["ok"])

        ok = handle.execute_read(unit)
        return Neo4jHealth(reachable=ok == 1, database=handle.settings.database)
    except Exception as exc:  # noqa: BLE001 — health is a report, not a raise
        return Neo4jHealth(
            reachable=False, database=handle.settings.database, reason=_sanitise(exc)
        )
