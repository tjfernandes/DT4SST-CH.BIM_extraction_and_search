"""Legacy `zeroentropy/zembed-1` @ 640 — evaluation only (HBIM-005B §12).

Never imported by ``api.*`` or ``ingestion.*``. ``sentence_transformers`` and
``torch`` are imported **inside** the call, so importing this module loads no ML
package and the unit suite, Ruff and mypy run without ``requirements-ml.txt``.

The call contract below is reproduced verbatim from the pre-HBIM-030 tree
(commit ``c0075bb~1``: ``api/search.py::_get_embedding_model`` /
``get_query_embedding`` and ``ingestion/index_to_opensearch.py::
get_embedding_model`` / ``generate_embeddings``). The baseline must describe the
legacy system *as it actually was*, so nothing here is modernised — in
particular no instruction prefix is applied, because the legacy path applied
none.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

__all__ = ["MODEL_ID", "TARGET_DIMENSIONS", "ZembedAdapter", "ZembedError"]

MODEL_ID = "zeroentropy/zembed-1"
TARGET_DIMENSIONS = 640

#: Pinned to the legacy default (``EMBEDDING_BATCH_SIZE``,
#: ``backend/shared/config.py``) rather than read from the environment: batch
#: shape can change bf16 kernel numerics, so it must be part of the record.
BATCH_SIZE = 2

#: Unit-norm tolerance for **bfloat16** output.
#:
#: The legacy contract loads the model in bf16 on CUDA, whose epsilon is
#: 2**-8 = 3.9e-3, so a genuinely normalised vector still lands off 1.0 by
#: roughly that much. Measured over this corpus: max deviation 3.15e-3, mean
#: 1.59e-3 — entirely bf16 rounding. A 1e-3 tolerance (correct for the fp16
#: service-side normalisation in HBIM-030) would reject correct output here,
#: while 1e-2 still catches an unnormalised vector by orders of magnitude.
#:
#: Ranking is unaffected either way: the runner divides by the measured norms
#: instead of assuming them (`_cosine`), so this is a validation bound only.
NORM_TOLERANCE = 1e-2


class ZembedError(RuntimeError):
    """The legacy model could not be pinned, loaded or validated."""


def _resolve_revision() -> tuple[str, bool, str, str]:
    """Return ``(revision, pinned, fingerprint, limitation)``.

    Step 1 of HBIM-005B §12.2: resolve the immutable 40-hex commit sha from the
    Hub. Step 2 is the fallback: if no immutable sha can be established, the
    caller must record a content fingerprint plus an explicit limitation. A
    floating model is never silently acceptable.
    """
    from huggingface_hub import HfApi

    # token=False: a stale on-disk token makes even public reads 401. The
    # baseline must not depend on the operator's credential state, and this
    # never reads or writes the token file.
    sha = HfApi(token=False).model_info(MODEL_ID).sha or ""
    if len(sha) == 40 and all(c in "0123456789abcdef" for c in sha):
        return sha, True, "", ""
    return (
        sha,
        False,
        "",
        f"{MODEL_ID} did not expose an immutable 40-hex revision; "
        "a content fingerprint is recorded instead",
    )


def _fingerprint(snapshot_dir: Path) -> str:
    """sha256 over the sorted ``[relative path, file sha256]`` table."""
    entries = []
    for path in sorted(p for p in snapshot_dir.rglob("*") if p.is_file()):
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        entries.append([path.relative_to(snapshot_dir).as_posix(), digest])
    payload = json.dumps(entries, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class ZembedAdapter:
    """Evaluation-only wrapper around the legacy SentenceTransformer path."""

    name = MODEL_ID
    role = "legacy_baseline"
    dimensions = TARGET_DIMENSIONS
    norm_tolerance = NORM_TOLERANCE

    def __init__(self) -> None:
        self._model: Any | None = None
        self._revision = ""
        self._revision_pinned = False
        self._fingerprint = ""
        self._limitation = ""
        self._snapshot = Path()
        self._used_encode_document = False
        self._used_encode_query = False

    # ------------------------------------------------------------------ #
    def _load(self) -> Any:
        if self._model is not None:
            return self._model

        revision, pinned, fingerprint, limitation = _resolve_revision()
        self._revision, self._revision_pinned = revision, pinned
        self._fingerprint, self._limitation = fingerprint, limitation

        from sentence_transformers import SentenceTransformer

        model_kwargs: dict[str, Any] = {}
        try:
            import torch

            if torch.cuda.is_available():
                model_kwargs["torch_dtype"] = torch.bfloat16
        except ImportError:  # pragma: no cover - torch is a hard dep of the ML profile
            pass

        # Materialise the pinned revision first, then load from that directory.
        # `modules.json` declares a custom `modeling_zembed.ZembedTransformer`,
        # and loading by repo id aborts before the remote code file is fetched.
        # Resolving the snapshot up front also removes network from load time
        # and makes the measured bytes explicit.
        from huggingface_hub import snapshot_download

        try:
            snapshot = snapshot_download(
                MODEL_ID, revision=revision or None, token=False
            )
        except Exception as exc:  # noqa: BLE001 — surfaced as a typed failure
            raise ZembedError(
                f"could not fetch {MODEL_ID}@{revision or 'HEAD'}: {type(exc).__name__}: {exc}"
            ) from exc

        init_kwargs: dict[str, Any] = {"trust_remote_code": True}
        if model_kwargs:
            init_kwargs["model_kwargs"] = model_kwargs

        try:
            self._model = SentenceTransformer(snapshot, **init_kwargs)
        except Exception as exc:  # noqa: BLE001 — surfaced as a typed failure
            raise ZembedError(f"could not load {MODEL_ID}: {type(exc).__name__}: {exc}") from exc
        self._snapshot = Path(snapshot)

        if not pinned:
            # Step 2 of §12.2: fingerprint the resolved snapshot so the artifact
            # still identifies exactly which bytes were measured.
            if not self._snapshot.is_dir():
                raise ZembedError("no immutable revision and no snapshot to fingerprint")
            self._fingerprint = _fingerprint(self._snapshot)
            if not self._fingerprint:
                raise ZembedError(
                    "no immutable revision and no content fingerprint could be computed"
                )
        return self._model

    # ------------------------------------------------------------------ #
    def provenance(self) -> dict[str, object]:
        self._load()
        return {
            "model_id": MODEL_ID,
            "role": self.role,
            "dimensions": self.dimensions,
            "batch_size": BATCH_SIZE,
            "revision": self._revision,
            "revision_pinned": self._revision_pinned,
            "model_content_fingerprint": self._fingerprint,
            "instruction_version": None,
            "used_encode_document": self._used_encode_document,
            "used_encode_query": self._used_encode_query,
            "limitation": self._limitation,
        }

    def _encode(self, texts: list[str], *, as_query: bool) -> list[list[float]]:
        model = self._load()
        kwargs: dict[str, Any] = {
            "convert_to_numpy": True,
            "normalize_embeddings": True,
            "truncate_dim": TARGET_DIMENSIONS,
        }
        if as_query:
            method = getattr(model, "encode_query", None)
            self._used_encode_query = method is not None
        else:
            kwargs["batch_size"] = BATCH_SIZE
            kwargs["show_progress_bar"] = False
            method = getattr(model, "encode_document", None)
            self._used_encode_document = method is not None
        if method is None:
            method = model.encode
        vectors = method(texts, **kwargs)
        out = [list(map(float, vector)) for vector in vectors]
        _validate(out, len(texts))
        return out

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """Documents, raw — the legacy path applied no instruction prefix."""
        return self._encode(list(texts), as_query=False)

    def embed_queries(self, texts: list[str]) -> list[list[float]]:
        """Queries through ``encode_query`` when available, exactly as the legacy
        API did; which branch ran is recorded in :meth:`provenance`."""
        return self._encode(list(texts), as_query=True)


def _validate(vectors: list[list[float]], expected: int) -> None:
    import math

    if len(vectors) != expected:
        raise ZembedError(f"expected {expected} vectors, got {len(vectors)}")
    for index, vector in enumerate(vectors):
        if len(vector) != TARGET_DIMENSIONS:
            raise ZembedError(f"vector {index} has {len(vector)} dims, expected {TARGET_DIMENSIONS}")
        if not all(math.isfinite(value) for value in vector):
            raise ZembedError(f"vector {index} contains a non-finite component")
        magnitude = math.sqrt(sum(value * value for value in vector))
        if abs(magnitude - 1.0) > NORM_TOLERANCE:
            raise ZembedError(f"vector {index} is not unit-norm (norm={magnitude:.6f})")
