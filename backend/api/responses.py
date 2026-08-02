"""HBIM-053 — grounded answer generation from the internal EvidencePack.

The only factual input to answer generation is a bounded projection of an
HBIM-052 :class:`~retrieval.evidence.EvidencePack`. This module owns the whole
grounded pipeline: projection, reference map, prompt assembly, strict output
parsing, structural support validation, deterministic abstention and the
deterministic renderer.

Three properties are load-bearing and are proven structurally, not asserted:

* **No ungrounded fallback.** Every failure path returns an abstention. There
  is no branch that produces free model text (spec §4 conflict C-4, §30).
* **No free-form answer field.** The model emits typed claim units; the final
  user-visible string is built here, from validated claims only (§21, §33).
* **No score is ever read.** HBIM-053 consumes whatever HBIM-051's accepted
  policy already placed in the pack; it introduces no cutoff (§32).

What this module does **not** do: prove semantic entailment. A verified quote
proves the cited text exists in the cited evidence, not that the claim follows
from it (§57.1).
"""

from __future__ import annotations

import json
import logging
import unicodedata
from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping, Protocol, Sequence

from retrieval.evidence import (
    EVIDENCE_PACK_VERSION,
    AggregateBucket,
    Caveat,
    EvidenceItem,
    EvidencePack,
)

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Versions (§14, §16, §21)
# --------------------------------------------------------------------------- #
GROUNDING_PROMPT_VERSION = "hbim-053-grounding-v1"
GROUNDING_PROJECTION_VERSION = "hbim-053-projection-v1"
GROUNDED_OUTPUT_VERSION = "hbim-053-output-v1"

# --------------------------------------------------------------------------- #
# Bounds (§38). No bound truncates; every breach abstains, so a citation can
# never be silently truncated away.
# --------------------------------------------------------------------------- #
MAX_ITEM_REFS = 50
MAX_AGG_REFS = 50
MAX_PROJECTION_BYTES = 131072
MAX_OUTPUT_BYTES = 65536
MAX_CLAIMS = 20
MAX_CLAIM_CHARS = 400
MAX_SUPPORTS_PER_CLAIM = 4
MAX_QUOTE_CHARS = 240
MIN_NORMALIZED_QUOTE_CHARS = 8
MAX_RENDERED_RESPONSE_CHARS = 12000

#: §21 — the only abstain reasons the model itself may emit.
MODEL_ABSTAIN_REASONS = frozenset({"insufficient_evidence", "out_of_scope"})


# --------------------------------------------------------------------------- #
# Errors (§40)
# --------------------------------------------------------------------------- #
class GroundingError(Exception):
    """Base for every grounding failure. None escapes generate_grounded_answer."""


class GroundingIdentityError(GroundingError):
    """Reference-map or identity defect."""


class GroundingLimitError(GroundingError):
    """A §38 bound was exceeded."""


class GroundedProviderUnavailable(GroundingError):
    """The provider could not be reached or failed."""


class GroundedResponseFormatUnsupported(GroundingError):
    """The provider rejected the required structured output.

    This is deliberately **not** recoverable by retrying as plain text: doing so
    is exactly the ungrounded fallback HBIM-053 exists to remove.
    """


# --------------------------------------------------------------------------- #
# Abstention (§31)
# --------------------------------------------------------------------------- #
class AbstentionReason(str, Enum):
    # pre-model (§29)
    NO_PACK = "no_pack"
    UNSUPPORTED_PACK_VERSION = "unsupported_pack_version"
    NO_EVIDENCE = "no_evidence"
    NO_USABLE_CONTENT = "no_usable_content"
    PROJECTION_TOO_LARGE = "projection_too_large"
    # post-model (§30)
    RESPONSE_FORMAT_UNSUPPORTED = "response_format_unsupported"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    OUTPUT_TOO_LARGE = "output_too_large"
    MALFORMED_OUTPUT = "malformed_output"
    SCHEMA_VIOLATION = "schema_violation"
    NO_CLAIMS = "no_claims"
    UNSUPPORTED_CLAIM = "unsupported_claim"
    UNKNOWN_REFERENCE = "unknown_reference"
    QUOTE_NOT_FOUND = "quote_not_found"
    AGGREGATE_MISMATCH = "aggregate_mismatch"
    MODEL_ABSTAINED = "model_abstained"
    RENDER_FAILURE = "render_failure"


#: §31 — the pre-existing empty-result string. Reused verbatim because an empty
#: result set is a *retrieval* outcome and "nothing matched" is strictly more
#: accurate than "insufficient evidence".
NO_RESULTS_MESSAGE = (
    "Não encontrei elementos que correspondam à sua pesquisa no modelo BIM."
)
ABSTENTION_NO_EVIDENCE_MESSAGE = (
    "Não encontrei evidência suficiente no modelo BIM para responder com fiabilidade."
)
ABSTENTION_UNAVAILABLE_MESSAGE = (
    "Não consegui gerar uma resposta fundamentada nesta evidência. "
    "Tente reformular a pergunta."
)

#: §31 — reason → user-facing prose. The reason code itself is never rendered.
_NO_RESULTS_REASONS = frozenset(
    {
        AbstentionReason.NO_PACK,
        AbstentionReason.NO_EVIDENCE,
        AbstentionReason.NO_USABLE_CONTENT,
    }
)


def abstention_message(reason: AbstentionReason) -> str:
    """§31 — exactly three messages, chosen by closed reason code."""
    if reason in _NO_RESULTS_REASONS:
        return NO_RESULTS_MESSAGE
    if reason is AbstentionReason.MODEL_ABSTAINED:
        return ABSTENTION_NO_EVIDENCE_MESSAGE
    return ABSTENTION_UNAVAILABLE_MESSAGE


# --------------------------------------------------------------------------- #
# Normalization helpers (§24, §33.1)
# --------------------------------------------------------------------------- #
def normalize_for_support(text: str) -> str:
    """§24 — closed, total, deterministic, idempotent.

    NFKC → casefold → collapse whitespace runs to one space → strip.
    """
    if not isinstance(text, str):
        raise GroundingIdentityError("normalize_for_support requires a string")
    folded = unicodedata.normalize("NFKC", text).casefold()
    return " ".join(folded.split())


def escape_markdown_text(text: str) -> str:
    """§33.1 — exact order: backslash, brackets, angle brackets.

    ``*`` and ``_`` are deliberately untouched. Escaping the brackets both makes
    ``[text](url)`` link injection impossible and guarantees that the only
    unescaped brackets in a rendered answer are the renderer's own citation
    markers, so ``[E001]`` is never ambiguous.
    """
    if not isinstance(text, str):
        raise GroundingIdentityError("escape_markdown_text requires a string")
    out = text.replace("\\", "\\\\")
    out = out.replace("[", "\\[").replace("]", "\\]")
    return out.replace("<", "&lt;").replace(">", "&gt;")


# --------------------------------------------------------------------------- #
# Reference map (§17-§20)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ItemRef:
    ref: str
    item: EvidenceItem


@dataclass(frozen=True)
class AggRef:
    ref: str
    agg_field: str
    bucket: AggregateBucket


@dataclass(frozen=True)
class ReferenceMap:
    """§18 — deterministic, collision-free, prompt-local.

    A reference is **not** a persistent source id. It maps back only to the pack
    it was built from, which is what makes cross-pack citation impossible rather
    than merely unlikely.
    """

    items: tuple[ItemRef, ...]
    aggregates: tuple[AggRef, ...]
    by_ref: Mapping[str, ItemRef | AggRef]

    @property
    def order(self) -> tuple[str, ...]:
        """Canonical reference order: every item ref, then every aggregate ref."""
        return tuple(entry.ref for entry in self.items) + tuple(
            entry.ref for entry in self.aggregates
        )


def build_reference_map(pack: EvidencePack) -> ReferenceMap:
    """§18 — ``E001..`` over ``pack.items``, then ``A001..`` over the buckets."""
    if len(pack.items) > MAX_ITEM_REFS:
        raise GroundingLimitError(f"more than {MAX_ITEM_REFS} item references")
    items = tuple(
        ItemRef(ref=f"E{index:03d}", item=item)
        for index, item in enumerate(pack.items, start=1)
    )

    aggregates: tuple[AggRef, ...] = ()
    if pack.aggregation is not None:
        buckets = pack.aggregation.buckets
        if len(buckets) > MAX_AGG_REFS:
            raise GroundingLimitError(f"more than {MAX_AGG_REFS} aggregate references")
        aggregates = tuple(
            AggRef(
                ref=f"A{index:03d}",
                agg_field=pack.aggregation.agg_field,
                bucket=bucket,
            )
            for index, bucket in enumerate(buckets, start=1)
        )

    by_ref: dict[str, ItemRef | AggRef] = {}
    entries: tuple[ItemRef | AggRef, ...] = (*items, *aggregates)
    for entry in entries:
        if entry.ref in by_ref:  # pragma: no cover - impossible by construction
            raise GroundingIdentityError(f"duplicate reference {entry.ref}")
        by_ref[entry.ref] = entry
    return ReferenceMap(items=items, aggregates=aggregates, by_ref=by_ref)


# --------------------------------------------------------------------------- #
# Projection (§16)
# --------------------------------------------------------------------------- #
def build_projection(
    pack: EvidencePack, question: str, refmap: ReferenceMap
) -> dict[str, Any]:
    """§16 — the bounded document handed to the model.

    The key set is closed and asserted by test. Nothing operational crosses
    this boundary: no ``index_identity``, no provenance, no score of any kind,
    no snapshot token, no vector, no raw ``_source``.
    """
    if not isinstance(question, str):
        raise GroundingIdentityError("question must be a string")

    projection: dict[str, Any] = {
        "projection_version": GROUNDING_PROJECTION_VERSION,
        "question": question,
        "route": pack.route,
        "result_count": pack.result_count,
        "total_hits": pack.total_hits,
        "result_from": pack.result_from,
        "caveats": [caveat.value for caveat in pack.caveats],
    }

    if refmap.items:
        evidence: list[dict[str, Any]] = []
        for entry in refmap.items:
            record: dict[str, Any] = {
                "ref": entry.ref,
                "source_kind": entry.item.source_kind.value,
                "source_id": entry.item.source_id,
                "content": entry.item.content,
                "content_truncated": entry.item.content_truncated,
            }
            if entry.item.project_id is not None:
                record["project_id"] = entry.item.project_id
            # §49 — a document item additionally exposes what a reader needs to
            # judge and cite the passage. Storage ids, revisions, link ids,
            # regions, index identity and every score stay out: the model never
            # needs them and they are not quotable.
            document = entry.item.document
            if document is not None:
                record["document_id"] = document.document_id
                record["page_number"] = document.page_number
                record["section_title"] = document.section_title
                record["ocr"] = document.ocr
            evidence.append(record)
        projection["evidence"] = evidence

    if pack.aggregation is not None:
        projection["aggregation"] = {
            "agg_field": pack.aggregation.agg_field,
            "total": pack.aggregation.total,
            "buckets": [
                {"ref": entry.ref, "key": entry.bucket.key, "count": entry.bucket.count}
                for entry in refmap.aggregates
            ],
        }
    return projection


def serialize_projection(projection: Mapping[str, Any]) -> str:
    """§15 — one JSON document. JSON escaping is what contains injection."""
    return json.dumps(projection, ensure_ascii=False, sort_keys=True)


# --------------------------------------------------------------------------- #
# Structured model output (§21-§23)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class SupportRecord:
    ref: str
    quote: str | None = None
    agg_key: str | None = None
    agg_count: int | None = None


@dataclass(frozen=True)
class ClaimUnit:
    text: str
    supports: tuple[SupportRecord, ...]


@dataclass(frozen=True)
class GroundedDraft:
    status: str
    claims: tuple[ClaimUnit, ...]
    abstain_reason: str | None


class DraftRejection(Exception):
    """Internal: carries the closed reason a draft was rejected with."""

    def __init__(self, reason: AbstentionReason) -> None:
        super().__init__(reason.value)
        self.reason = reason


_CLAIM_KEYS = frozenset({"text", "supports"})
_SUPPORT_KEYS = frozenset({"ref", "quote", "agg_key", "agg_count"})
_DRAFT_KEYS = frozenset({"status", "claims", "abstain_reason"})


def _reject(reason: AbstentionReason) -> "DraftRejection":
    return DraftRejection(reason)


def _require_str(value: object) -> str:
    if isinstance(value, bool) or not isinstance(value, str):
        raise _reject(AbstentionReason.SCHEMA_VIOLATION)
    return value


def parse_draft(raw: str) -> GroundedDraft:
    """§21-§23 — strict parse. Anything unexpected raises ``DraftRejection``."""
    if not isinstance(raw, str):
        raise _reject(AbstentionReason.MALFORMED_OUTPUT)
    if len(raw.encode("utf-8")) > MAX_OUTPUT_BYTES:
        raise _reject(AbstentionReason.OUTPUT_TOO_LARGE)
    try:
        payload = json.loads(raw)
    except (ValueError, TypeError):
        raise _reject(AbstentionReason.MALFORMED_OUTPUT) from None
    if not isinstance(payload, dict):
        raise _reject(AbstentionReason.SCHEMA_VIOLATION)
    if not set(payload).issubset(_DRAFT_KEYS):
        raise _reject(AbstentionReason.SCHEMA_VIOLATION)

    status = _require_str(payload.get("status"))
    if status not in ("answer", "abstain"):
        raise _reject(AbstentionReason.SCHEMA_VIOLATION)

    if status == "abstain":
        reason = payload.get("abstain_reason")
        if not isinstance(reason, str) or reason not in MODEL_ABSTAIN_REASONS:
            raise _reject(AbstentionReason.SCHEMA_VIOLATION)
        return GroundedDraft(status=status, claims=(), abstain_reason=reason)

    if payload.get("abstain_reason") is not None:
        raise _reject(AbstentionReason.SCHEMA_VIOLATION)
    raw_claims = payload.get("claims")
    if not isinstance(raw_claims, list):
        raise _reject(AbstentionReason.SCHEMA_VIOLATION)
    if not raw_claims:
        raise _reject(AbstentionReason.NO_CLAIMS)
    if len(raw_claims) > MAX_CLAIMS:
        raise _reject(AbstentionReason.SCHEMA_VIOLATION)

    claims = tuple(_parse_claim(entry) for entry in raw_claims)
    return GroundedDraft(status=status, claims=claims, abstain_reason=None)


def _parse_claim(entry: object) -> ClaimUnit:
    if not isinstance(entry, dict) or not set(entry).issubset(_CLAIM_KEYS):
        raise _reject(AbstentionReason.SCHEMA_VIOLATION)
    text = _require_str(entry.get("text"))
    if not text.strip() or len(text) > MAX_CLAIM_CHARS:
        raise _reject(AbstentionReason.SCHEMA_VIOLATION)

    raw_supports = entry.get("supports")
    if not isinstance(raw_supports, list):
        raise _reject(AbstentionReason.SCHEMA_VIOLATION)
    if not raw_supports:
        raise _reject(AbstentionReason.UNSUPPORTED_CLAIM)
    if len(raw_supports) > MAX_SUPPORTS_PER_CLAIM:
        raise _reject(AbstentionReason.SCHEMA_VIOLATION)

    supports = tuple(_parse_support(item) for item in raw_supports)
    seen: set[str] = set()
    for support in supports:
        # §26 — a repeated ref inside one claim adds no evidence and would
        # double-render; it is a schema violation, not a silent dedup.
        if support.ref in seen:
            raise _reject(AbstentionReason.SCHEMA_VIOLATION)
        seen.add(support.ref)
    return ClaimUnit(text=text, supports=supports)


def _parse_support(entry: object) -> SupportRecord:
    if not isinstance(entry, dict) or not set(entry).issubset(_SUPPORT_KEYS):
        raise _reject(AbstentionReason.SCHEMA_VIOLATION)
    ref = _require_str(entry.get("ref"))
    quote = entry.get("quote")
    agg_key = entry.get("agg_key")
    agg_count = entry.get("agg_count")

    is_item = len(ref) == 4 and ref[0] == "E" and ref[1:].isdigit()
    is_agg = len(ref) == 4 and ref[0] == "A" and ref[1:].isdigit()
    if not (is_item or is_agg):
        raise _reject(AbstentionReason.UNKNOWN_REFERENCE)

    if is_item:
        if agg_key is not None or agg_count is not None:
            raise _reject(AbstentionReason.SCHEMA_VIOLATION)
        text = _require_str(quote)
        if not text.strip() or len(text) > MAX_QUOTE_CHARS:
            raise _reject(AbstentionReason.SCHEMA_VIOLATION)
        return SupportRecord(ref=ref, quote=text)

    if quote is not None:
        raise _reject(AbstentionReason.SCHEMA_VIOLATION)
    key = _require_str(agg_key)
    if isinstance(agg_count, bool) or not isinstance(agg_count, int):
        raise _reject(AbstentionReason.SCHEMA_VIOLATION)
    return SupportRecord(ref=ref, agg_key=key, agg_count=agg_count)


# --------------------------------------------------------------------------- #
# Support validation (§24-§26)
# --------------------------------------------------------------------------- #
def validate_item_support(support: SupportRecord, item: EvidenceItem) -> None:
    """§24 — the quote must exist in **this** item's bounded content."""
    quote = support.quote or ""
    normalized_quote = normalize_for_support(quote)
    if len(normalized_quote) < MIN_NORMALIZED_QUOTE_CHARS:
        raise _reject(AbstentionReason.QUOTE_NOT_FOUND)
    if normalized_quote not in normalize_for_support(item.content):
        raise _reject(AbstentionReason.QUOTE_NOT_FOUND)


def validate_aggregate_support(support: SupportRecord, entry: AggRef) -> None:
    """§25 — exact equality on field, key and count. No tolerance, no coercion."""
    if support.agg_key != entry.bucket.key:
        raise _reject(AbstentionReason.AGGREGATE_MISMATCH)
    if support.agg_count != entry.bucket.count:
        raise _reject(AbstentionReason.AGGREGATE_MISMATCH)


def validate_draft(draft: GroundedDraft, refmap: ReferenceMap) -> None:
    """§26/§30 — all-or-nothing.

    One invalid claim abstains the whole response: a model that fabricated one
    claim has already shown it is not constrained by the evidence, so rendering
    its other claims would be trusting exactly what just failed.
    """
    for claim in draft.claims:
        if not claim.supports:  # pragma: no cover - parse already rejects this
            raise _reject(AbstentionReason.UNSUPPORTED_CLAIM)
        for support in claim.supports:
            entry = refmap.by_ref.get(support.ref)
            if entry is None:
                raise _reject(AbstentionReason.UNKNOWN_REFERENCE)
            if isinstance(entry, ItemRef):
                if support.quote is None:
                    raise _reject(AbstentionReason.SCHEMA_VIOLATION)
                validate_item_support(support, entry.item)
            else:
                if support.agg_key is None or support.agg_count is None:
                    raise _reject(AbstentionReason.SCHEMA_VIOLATION)
                validate_aggregate_support(support, entry)


# --------------------------------------------------------------------------- #
# Citations (§35)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Citation:
    ref: str
    kind: str
    source_kind: str | None = None
    source_id: str | None = None
    project_id: str | None = None
    agg_field: str | None = None
    agg_key: str | None = None
    agg_count: int | None = None
    # HBIM-073 §47 — document provenance. ``storage_chunk_id`` is internal-only
    # (decision AX): it is retained here for audit and snapshot verification and
    # is deliberately NOT projected into ``PublicCitation``.
    document_id: str | None = None
    base_chunk_id: str | None = None
    storage_chunk_id: str | None = None
    page_number: int | None = None
    page_span: tuple[int, int] | None = None
    section_title: str | None = None
    ocr: bool | None = None


def build_citations(
    draft: GroundedDraft, refmap: ReferenceMap
) -> tuple[Citation, ...]:
    """§35 — every ref actually cited, in reference-map order, deduplicated."""
    cited = {support.ref for claim in draft.claims for support in claim.supports}
    citations: list[Citation] = []
    for ref in refmap.order:
        if ref not in cited:
            continue
        entry = refmap.by_ref[ref]
        if isinstance(entry, ItemRef):
            document = entry.item.document
            citations.append(
                Citation(
                    ref=ref,
                    kind="item",
                    source_kind=entry.item.source_kind.value,
                    source_id=entry.item.source_id,
                    project_id=entry.item.project_id,
                    # Filled by the server from the validated evidence item
                    # (§48) — never from model output.
                    document_id=None if document is None else document.document_id,
                    base_chunk_id=None if document is None else document.base_chunk_id,
                    storage_chunk_id=None if document is None else document.storage_chunk_id,
                    page_number=None if document is None else document.page_number,
                    page_span=None if document is None else document.page_span,
                    section_title=None if document is None else document.section_title,
                    ocr=None if document is None else document.ocr,
                )
            )
        else:
            # §20 — a bucket has no source_id and none is invented.
            citations.append(
                Citation(
                    ref=ref,
                    kind="aggregate",
                    agg_field=entry.agg_field,
                    agg_key=entry.bucket.key,
                    agg_count=entry.bucket.count,
                )
            )
    return tuple(citations)


# --------------------------------------------------------------------------- #
# Renderer (§33-§34)
# --------------------------------------------------------------------------- #
def document_citation_labels(
    draft: GroundedDraft, refmap: ReferenceMap
) -> tuple[str, ...]:
    """§48 — ``(documento <document_id>, página <n>)`` per cited document item.

    Deterministic and server-filled: reference-map order, deduplicated, and
    every value read from the validated evidence item. A page is omitted only
    when the chunk genuinely carries none — never guessed.
    """
    cited = {support.ref for claim in draft.claims for support in claim.supports}
    labels: list[str] = []
    seen: set[tuple[str, int | None]] = set()
    for ref in refmap.order:
        if ref not in cited:
            continue
        entry = refmap.by_ref[ref]
        document = getattr(getattr(entry, "item", None), "document", None)
        if document is None:
            continue
        key = (document.document_id, document.page_number)
        if key in seen:
            continue
        seen.add(key)
        if document.page_number is None:
            labels.append(f"({ref}: documento {document.document_id})")
        else:
            labels.append(
                f"({ref}: documento {document.document_id}, "
                f"página {document.page_number})"
            )
    return tuple(labels)


def render_answer(
    draft: GroundedDraft, refmap: ReferenceMap, pack: EvidencePack
) -> str:
    """§33 — pure. Same ``(draft, refmap, pack)`` → byte-identical string."""
    order = {ref: index for index, ref in enumerate(refmap.order)}
    blocks: list[str] = []
    for claim in draft.claims:
        refs = sorted({support.ref for support in claim.supports}, key=order.__getitem__)
        marker = "[" + ", ".join(refs) + "]"
        blocks.append(f"{escape_markdown_text(claim.text)} {marker}")

    rendered = "\n\n".join(blocks)
    # §48 — one deterministic label per cited document item, filled by the
    # server from validated evidence. The model emits only ``[E00n]`` markers;
    # no document or page value is ever model-written.
    labels = document_citation_labels(draft, refmap)
    if labels:
        rendered += "\n\n" + "\n".join(labels)
    if pack.total_hits is not None and pack.total_hits > pack.result_count:
        rendered += (
            f"\n\n_A mostrar {pack.result_count} de {pack.total_hits} resultados._"
        )
    if Caveat.LEGACY_SOURCE in pack.caveats:
        rendered += "\n\n_Fonte: índice legado._"

    # §33.2 — never truncate: truncation could drop a citation marker.
    if len(rendered) > MAX_RENDERED_RESPONSE_CHARS:
        raise GroundingLimitError("rendered response exceeds the bound")
    return rendered


# --------------------------------------------------------------------------- #
# Provider adapter (§27-§28)
# --------------------------------------------------------------------------- #
class GroundedLLM(Protocol):
    """§27/§43 — the injected seam. No test may use a live model."""

    def complete(self, messages: list[dict[str, str]]) -> str: ...


class OpenAIGroundedLLM:
    """§27 — one call, structured output required, no text fallback.

    Deliberately does **not** reuse ``api.search.get_response``: that helper
    prepends a generic system prompt, forwards conversation history, logs whole
    prompts and outputs under the legacy flags, and — decisively — strips
    ``response_format`` and retries as free text when the provider rejects it.
    Every one of those is disqualifying here (spec §4 conflict C-4, §39).
    """

    def complete(self, messages: list[dict[str, str]]) -> str:
        from openai import BadRequestError

        from api.search import LLM_MODEL, get_llm_client

        client = get_llm_client()
        try:
            response = client.chat.completions.create(
                model=LLM_MODEL,
                messages=messages,
                temperature=0,
                response_format={"type": "json_object"},
            )
        except BadRequestError as exc:
            raise GroundedResponseFormatUnsupported(
                "provider rejected the required structured output"
            ) from exc
        except Exception as exc:
            raise GroundedProviderUnavailable("provider call failed") from exc

        content = getattr(response.choices[0].message, "content", None)
        if not isinstance(content, str):
            raise GroundedProviderUnavailable("provider returned no text content")
        return content


def default_grounded_llm() -> GroundedLLM:
    """§41 — constructed lazily; no client exists at import time."""
    return OpenAIGroundedLLM()


def build_messages(projection: Mapping[str, Any]) -> list[dict[str, str]]:
    """§14 — exactly two messages: system contract, then one JSON data document."""
    from api.prompts import GROUNDED_ANSWER_CONTRACT

    return [
        {"role": "system", "content": GROUNDED_ANSWER_CONTRACT},
        {"role": "user", "content": serialize_projection(projection)},
    ]


# --------------------------------------------------------------------------- #
# Outcome (§36, §39)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class GroundedOutcome:
    status: str  # "answer" | "abstained"  (§31: the model says "abstain")
    text: str
    citations: tuple[Citation, ...]
    abstention_reason: AbstentionReason | None
    claim_count: int
    provider_calls: int
    projection_bytes: int
    item_ref_count: int
    agg_ref_count: int

    @property
    def abstained(self) -> bool:
        return self.status == "abstained"


def _abstain(
    reason: AbstentionReason,
    *,
    provider_calls: int = 0,
    projection_bytes: int = 0,
    item_ref_count: int = 0,
    agg_ref_count: int = 0,
) -> GroundedOutcome:
    return GroundedOutcome(
        status="abstained",
        text=abstention_message(reason),
        citations=(),
        abstention_reason=reason,
        claim_count=0,
        provider_calls=provider_calls,
        projection_bytes=projection_bytes,
        item_ref_count=item_ref_count,
        agg_ref_count=agg_ref_count,
    )


def _has_usable_evidence(pack: EvidencePack) -> bool:
    """§29 rule 4 — usable means quotable content or an aggregation bucket."""
    if any(item.content.strip() for item in pack.items):
        return True
    return pack.aggregation is not None and bool(pack.aggregation.buckets)


def generate_grounded_answer(
    pack: EvidencePack | None,
    question: str,
    llm: GroundedLLM,
) -> GroundedOutcome:
    """§29/§30 — the single entry point. Never raises; always returns an outcome.

    Exactly one provider call on the success path, zero on every pre-model
    abstention, and never a second call: there is no retry, no repair model and
    no model asked to validate its own output.
    """
    # ---- pre-model (§29), in the specified order ------------------------- #
    if pack is None:
        return _abstain(AbstentionReason.NO_PACK)
    if pack.version != EVIDENCE_PACK_VERSION:
        return _abstain(AbstentionReason.UNSUPPORTED_PACK_VERSION)
    if Caveat.NO_EVIDENCE in pack.caveats:
        return _abstain(AbstentionReason.NO_EVIDENCE)
    if not _has_usable_evidence(pack):
        return _abstain(AbstentionReason.NO_USABLE_CONTENT)

    try:
        refmap = build_reference_map(pack)
        projection = build_projection(pack, question, refmap)
        payload = serialize_projection(projection)
    except GroundingLimitError:
        return _abstain(AbstentionReason.PROJECTION_TOO_LARGE)
    except GroundingError:
        return _abstain(AbstentionReason.PROJECTION_TOO_LARGE)

    projection_bytes = len(payload.encode("utf-8"))
    counts = {
        "projection_bytes": projection_bytes,
        "item_ref_count": len(refmap.items),
        "agg_ref_count": len(refmap.aggregates),
    }
    if projection_bytes > MAX_PROJECTION_BYTES:
        return _abstain(AbstentionReason.PROJECTION_TOO_LARGE, **counts)

    # ---- the single model call (§28) ------------------------------------- #
    try:
        raw = llm.complete(build_messages(projection))
    except GroundedResponseFormatUnsupported:
        return _abstain(
            AbstentionReason.RESPONSE_FORMAT_UNSUPPORTED, provider_calls=1, **counts
        )
    except Exception:
        return _abstain(
            AbstentionReason.PROVIDER_UNAVAILABLE, provider_calls=1, **counts
        )

    # ---- post-model (§30) ------------------------------------------------ #
    try:
        draft = parse_draft(raw)
        if draft.status == "abstain":
            raise _reject(AbstentionReason.MODEL_ABSTAINED)
        validate_draft(draft, refmap)
        text = render_answer(draft, refmap, pack)
    except DraftRejection as rejection:
        return _abstain(rejection.reason, provider_calls=1, **counts)
    except GroundingError:
        return _abstain(AbstentionReason.RENDER_FAILURE, provider_calls=1, **counts)

    return GroundedOutcome(
        status="answer",
        text=text,
        citations=build_citations(draft, refmap),
        abstention_reason=None,
        claim_count=len(draft.claims),
        provider_calls=1,
        **counts,
    )


# --------------------------------------------------------------------------- #
# Observability (§39)
# --------------------------------------------------------------------------- #
def observability_event(outcome: GroundedOutcome) -> dict[str, Any]:
    """§39 — closed codes and integers only. No text of any kind."""
    return {
        "grounding_status": outcome.status,
        "abstention_reason": (
            outcome.abstention_reason.value
            if outcome.abstention_reason is not None
            else None
        ),
        "claim_count": outcome.claim_count,
        "citation_count": len(outcome.citations),
        "item_ref_count": outcome.item_ref_count,
        "agg_ref_count": outcome.agg_ref_count,
        "projection_bytes": outcome.projection_bytes,
        "provider_calls": outcome.provider_calls,
    }


__all__: Sequence[str] = (
    "document_citation_labels",
    "GROUNDING_PROMPT_VERSION",
    "GROUNDING_PROJECTION_VERSION",
    "GROUNDED_OUTPUT_VERSION",
    "AbstentionReason",
    "Citation",
    "ClaimUnit",
    "GroundedDraft",
    "GroundedLLM",
    "GroundedOutcome",
    "GroundedProviderUnavailable",
    "GroundedResponseFormatUnsupported",
    "GroundingError",
    "GroundingIdentityError",
    "GroundingLimitError",
    "ReferenceMap",
    "SupportRecord",
    "abstention_message",
    "build_citations",
    "build_messages",
    "build_projection",
    "build_reference_map",
    "default_grounded_llm",
    "escape_markdown_text",
    "generate_grounded_answer",
    "normalize_for_support",
    "observability_event",
    "parse_draft",
    "render_answer",
    "serialize_projection",
    "validate_draft",
)
