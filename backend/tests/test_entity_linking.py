"""HBIM-072 §7-§26 — the pure deterministic linker.

Anti-tautology: every expected element, method, outcome and offset here is a
hand-written literal derived from the specification's *rules*, never captured
from the linker's output. The gold corpus is authored independently
(`eval/dataset/entity_linking_gold.jsonl`) and replayed by
`test_entity_linking_eval.py`.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from ingestion.entity_linking import (
    ELEMENT_ID_RE,
    FUZZY_MIN_MARGIN,
    FUZZY_MIN_SCORE,
    GLOBAL_ID_RE,
    LINK_CONFIG_FINGERPRINT,
    LINKER_NORMALIZATION_VERSION,
    LINKER_VERSION,
    MAX_CATALOG_ELEMENTS,
    MAX_FUZZY_CANDIDATES_PER_MENTION,
    MAX_LINKS_PER_CHUNK,
    MAX_NAME_TOKENS,
    MIN_ELIGIBLE_NAME_CHARS,
    STOP_NAMES,
    CatalogBoundsError,
    CatalogProjectMismatchError,
    DuplicateElementError,
    LinkInputError,
    MentionOutcome,
    build_catalog,
    is_eligible_name,
    link_chunk,
    load_catalog,
    osa_distance,
    similarity,
    tokenize,
)

BACKEND = Path(__file__).resolve().parents[1]
GOLD = BACKEND / "eval" / "dataset" / "entity_linking_gold.jsonl"


def _gold_rows() -> list[dict]:
    return [json.loads(line) for line in GOLD.read_text(encoding="utf-8").splitlines() if line.strip()]


@pytest.fixture(scope="module")
def catalog_records() -> list[dict]:
    row = next(r for r in _gold_rows() if r["kind"] == "catalog" and r["catalog_id"] == "full")
    return [e for e in row["elements"] if e["project_id"] == "proj-lnk"]


@pytest.fixture(scope="module")
def other_records() -> list[dict]:
    row = next(r for r in _gold_rows() if r["kind"] == "catalog" and r["catalog_id"] == "full")
    return [e for e in row["elements"] if e["project_id"] == "proj-other"]


@pytest.fixture(scope="module")
def catalog(catalog_records):
    return build_catalog(catalog_records, project_id="proj-lnk")


def element_of(catalog, name: str, storey: str | None = None) -> str:
    matches = [
        e for e in catalog.elements
        if e.name == name and (storey is None or e.storey_name == storey)
    ]
    assert len(matches) == 1, (name, storey, [e.element_id for e in matches])
    return matches[0].element_id


def link_ids(result) -> list[str]:
    return [link.element_id for link in result.links]


# --------------------------------------------------------------------------- #
# §9 — normalisation and half-open original offsets
# --------------------------------------------------------------------------- #
def test_tokenize_folds_accents_and_keeps_original_offsets() -> None:
    text = "A «Muralha Norte» — inspecção."
    tokens = tokenize(text)
    assert [(t.text, t.start, t.end) for t in tokens] == [
        ("a", 0, 1), ("muralha", 3, 10), ("norte", 11, 16), ("inspeccao", 20, 29),
    ]
    for token in tokens:
        assert text[token.start:token.end]  # spans are real, half-open slices
    assert text[tokens[1].start:tokens[2].end] == "Muralha Norte"


def test_tokenize_handles_decomposition_and_separators() -> None:
    assert [(t.text, t.start, t.end) for t in tokenize("Ábside Poente")] == [
        ("abside", 0, 6), ("poente", 7, 13),
    ]
    assert [(t.text, t.start, t.end) for t in tokenize("co-  operação")] == [
        ("co", 0, 2), ("operacao", 5, 13),
    ]
    assert tokenize("") == ()
    assert tokenize("   —  ") == ()


def test_tokenize_rejects_non_strings() -> None:
    with pytest.raises(TypeError):
        tokenize(None)  # type: ignore[arg-type]


def test_combining_marks_are_dropped_not_treated_as_separators() -> None:
    """§9 — a combining mark modifies its base letter; it never splits a word.

    Decomposed (NFD) text is common in extracted/OCR'd PDFs, so `Á` written as
    `A` + U+0301 must tokenize exactly like the precomposed form, and the span
    must still slice the ORIGINAL text.
    """
    decomposed = "Ábside Poente"
    precomposed = "Ábside Poente"
    assert [t.text for t in tokenize(decomposed)] == [t.text for t in tokenize(precomposed)]
    first = tokenize(decomposed)[0]
    assert first.text == "abside"
    assert decomposed[first.start:first.end] == "Ábside"
    # a trailing mark stays inside its word, and a leading stray mark is skipped
    assert [t.text for t in tokenize("abć def")] == ["abc", "def"]
    assert [t.text for t in tokenize("́abc")] == ["abc"]


def test_decomposed_accents_still_match_exactly(catalog) -> None:
    """The method must be `exact_name`, never a fuzzy near-miss."""
    result = link_chunk("A Ábside Poente foi consolidada.",
                        catalog=catalog, project_id="proj-lnk")
    assert link_ids(result) == [element_of(catalog, "Ábside Poente")]
    assert result.links[0].method.value == "exact_name"
    mention = result.links[0].mentions[0]
    assert mention.text == "Ábside Poente"


def test_normalization_version_is_pinned() -> None:
    assert LINKER_NORMALIZATION_VERSION == "hbim-072-normalization-v1"
    assert LINKER_VERSION == "hbim-072-linker-v1"


def test_linker_normalisation_differs_deliberately_from_the_other_contracts() -> None:
    """§9 — a fourth contract, never a silent mix of the existing three."""
    from ingestion.ifc_values import normalize_lexical
    from retrieval.router import normalize_query

    # ifc_values keeps accents; the linker folds them.
    assert normalize_lexical("Ábside") == "ábside"
    assert " ".join(t.text for t in tokenize("Ábside")) == "abside"
    # router returns a string and keeps underscores; the linker returns tokens
    # with offsets and splits on underscore (hence §10's regexes).
    assert normalize_query("el_1a2b") == "el_1a2b"
    assert [t.text for t in tokenize("el_1a2b")] == ["el", "1a2b"]


# --------------------------------------------------------------------------- #
# §7/§8 — catalog, isolation, fingerprint
# --------------------------------------------------------------------------- #
def test_catalog_projects_only_the_linking_relevant_fields(catalog) -> None:
    element = next(e for e in catalog.elements if e.name == "Muralha Norte")
    assert element.project_id == "proj-lnk"
    assert element.ifc_class == "IfcWall"
    assert element.storey_name == "Piso 0"
    assert element.building_name == "Castelo"
    assert element.material_names == ("Granito",)
    assert not hasattr(element, "description")
    assert not hasattr(element, "metrics")


def test_catalog_is_sorted_by_element_id(catalog) -> None:
    ids = [e.element_id for e in catalog.elements]
    assert ids == sorted(ids)


def test_foreign_project_record_is_rejected_never_filtered(
    catalog_records, other_records
) -> None:
    with pytest.raises(CatalogProjectMismatchError):
        build_catalog(catalog_records + other_records, project_id="proj-lnk")


def test_duplicate_element_id_and_global_id_are_rejected(catalog_records) -> None:
    duplicate_id = json.loads(json.dumps(catalog_records[0]))
    with pytest.raises(DuplicateElementError):
        build_catalog(catalog_records + [duplicate_id], project_id="proj-lnk")

    clashing_gid = json.loads(json.dumps(catalog_records[0]))
    clashing_gid["element_id"] = "el_" + "f" * 32
    with pytest.raises(DuplicateElementError):
        build_catalog(catalog_records + [clashing_gid], project_id="proj-lnk")


def test_catalog_bound_is_enforced(catalog_records, monkeypatch) -> None:
    import ingestion.entity_linking as module

    monkeypatch.setattr(module, "MAX_CATALOG_ELEMENTS", 2)
    with pytest.raises(CatalogBoundsError):
        build_catalog(catalog_records, project_id="proj-lnk")


def test_bounds_are_the_committed_constants() -> None:
    assert MAX_CATALOG_ELEMENTS == 200_000
    assert MAX_NAME_TOKENS == 8
    assert MIN_ELIGIBLE_NAME_CHARS == 4
    assert MAX_FUZZY_CANDIDATES_PER_MENTION == 200
    assert MAX_LINKS_PER_CHUNK == 32
    assert (FUZZY_MIN_SCORE, FUZZY_MIN_MARGIN) == (0.85, 0.10)


def test_fingerprint_is_order_independent(catalog_records) -> None:
    forward = build_catalog(catalog_records, project_id="proj-lnk").fingerprint
    backward = build_catalog(list(reversed(catalog_records)), project_id="proj-lnk").fingerprint
    assert forward == backward
    assert forward.startswith("cat_")


def test_fingerprint_changes_on_every_relevant_field(catalog_records) -> None:
    base = build_catalog(catalog_records, project_id="proj-lnk").fingerprint
    for field, value in (
        ("name", "Outro Nome"), ("ifc_class", "IfcBeam"), ("global_id", "0" * 22),
        ("object_type", "tipo"), ("predefined_type", "PRE"), ("semantic_label", "sem"),
    ):
        mutated = json.loads(json.dumps(catalog_records))
        mutated[0][field] = value
        changed = build_catalog(mutated, project_id="proj-lnk").fingerprint
        assert changed != base, field

    for field, value in (("storey", "Piso 9"), ("space", "Sala X"), ("building", "Ala Z")):
        mutated = json.loads(json.dumps(catalog_records))
        mutated[0]["location"][field] = {"name": value}
        assert build_catalog(mutated, project_id="proj-lnk").fingerprint != base, field

    mutated = json.loads(json.dumps(catalog_records))
    mutated[0]["materials"] = [{"name": "Betao", "ordinal": 0}]
    assert build_catalog(mutated, project_id="proj-lnk").fingerprint != base


def test_fingerprint_ignores_irrelevant_fields(catalog_records) -> None:
    """§8 — minimal: an irrelevant change must NOT force a relink."""
    base = build_catalog(catalog_records, project_id="proj-lnk").fingerprint
    mutated = json.loads(json.dumps(catalog_records))
    mutated[0]["description"] = "uma descricao nova"
    mutated[0]["metrics"] = {"area": 12.5}
    mutated[0]["source"] = {"source_id": "outro"}
    assert build_catalog(mutated, project_id="proj-lnk").fingerprint == base


def test_empty_catalog_is_legal(catalog) -> None:
    empty = build_catalog([], project_id="proj-lnk")
    assert empty.elements == ()
    assert empty.fingerprint != catalog.fingerprint
    result = link_chunk("A Muralha Norte apresenta erosao.", catalog=empty,
                        project_id="proj-lnk")
    assert result.links == ()


def test_load_catalog_reads_jsonl(tmp_path: Path, catalog_records) -> None:
    target = tmp_path / "elements.jsonl"
    target.write_text(
        "".join(json.dumps(r, sort_keys=True) + "\n" for r in catalog_records),
        encoding="utf-8",
    )
    loaded = load_catalog(target, project_id="proj-lnk")
    assert len(loaded.elements) == len(catalog_records)
    assert loaded.fingerprint == build_catalog(catalog_records, project_id="proj-lnk").fingerprint


# --------------------------------------------------------------------------- #
# §10 — exact identity
# --------------------------------------------------------------------------- #
def test_global_id_pattern_matches_the_project_wide_contract() -> None:
    """§10 — one GlobalId contract, asserted rather than imported (layering)."""
    from retrieval.router import GLOBAL_ID_RE as ROUTER_RE

    assert GLOBAL_ID_RE.pattern == ROUTER_RE.pattern


def test_element_id_and_global_id_link_exactly(catalog) -> None:
    target = element_of(catalog, "Muralha Norte")
    gid = next(e.global_id for e in catalog.elements if e.element_id == target)

    by_id = link_chunk(f"Ver {target} no anexo.", catalog=catalog, project_id="proj-lnk")
    assert link_ids(by_id) == [target]
    assert by_id.links[0].method.value == "element_id"
    assert by_id.links[0].score == 1.0
    assert by_id.links[0].runner_up_score is None

    by_gid = link_chunk(f"O elemento {gid} cedeu.", catalog=catalog, project_id="proj-lnk")
    assert link_ids(by_gid) == [target]
    assert by_gid.links[0].method.value == "global_id"


def test_unknown_identifiers_never_fall_through_to_fuzzy(catalog) -> None:
    gid = next(e.global_id for e in catalog.elements if e.name == "Muralha Norte")
    result = link_chunk(
        f"Ver el_{'0' * 32} e {gid.lower()} aqui.", catalog=catalog, project_id="proj-lnk"
    )
    assert result.links == ()
    assert [m.outcome for m in result.mentions] == [
        MentionOutcome.UNRESOLVED_UNKNOWN_IDENTIFIER,
        MentionOutcome.UNRESOLVED_UNKNOWN_IDENTIFIER,
    ]


def test_identifier_token_boundaries(catalog) -> None:
    target = element_of(catalog, "Muralha Norte")
    gid = next(e.global_id for e in catalog.elements if e.element_id == target)
    assert ELEMENT_ID_RE.findall(f"({target}),") == [target]
    assert ELEMENT_ID_RE.findall(f"x{target}") == []
    assert ELEMENT_ID_RE.findall(target.upper()) == []
    assert GLOBAL_ID_RE.findall(f"ref={gid},") == [gid]
    assert GLOBAL_ID_RE.findall(f"{gid}X") == []


def test_identifier_span_is_consumed_before_name_matching(catalog) -> None:
    """§10 — an identifier's characters can never also be read as a name."""
    target = element_of(catalog, "Muralha Norte")
    result = link_chunk(f"Ver {target} agora.", catalog=catalog, project_id="proj-lnk")
    assert len(result.links) == 1
    assert len(result.links[0].mentions) == 1


# --------------------------------------------------------------------------- #
# §11 — eligible names, stop names, overlap
# --------------------------------------------------------------------------- #
def test_stop_names_and_short_names_are_ineligible() -> None:
    assert not is_eligible_name("Parede")
    assert not is_eligible_name("porta")
    assert not is_eligible_name("P1")
    assert not is_eligible_name("")
    assert is_eligible_name("Muralha Norte")
    assert is_eligible_name("Sala 101")
    assert "parede" in STOP_NAMES and "wall" in STOP_NAMES


def test_generic_and_short_names_never_link(catalog) -> None:
    generic = link_chunk("A parede esta degradada junto ao piso.",
                         catalog=catalog, project_id="proj-lnk")
    assert generic.links == ()
    short = link_chunk("A coluna P1 foi medida no local.",
                       catalog=catalog, project_id="proj-lnk")
    assert short.links == ()


def test_exact_name_matching_is_token_sequence_not_substring(catalog) -> None:
    """§9 — `Porta` must not match inside `portada`."""
    result = link_chunk("A portada é antiga e a paredeiro tambem.",
                        catalog=catalog, project_id="proj-lnk")
    assert result.links == ()


def test_accents_and_punctuation_do_not_block_exact_names(catalog) -> None:
    for text, name in (
        ("A Ábside Poente foi consolidada.", "Ábside Poente"),
        ("A «Muralha Norte» — inspeccao.", "Muralha Norte"),
        ("A ABSIDE POENTE foi consolidada.", "Ábside Poente"),
    ):
        result = link_chunk(text, catalog=catalog, project_id="proj-lnk")
        assert link_ids(result) == [element_of(catalog, name)], text
        mention = result.links[0].mentions[0]
        assert text[mention.start:mention.end] == mention.text


def test_longest_match_wins_and_matches_never_overlap(catalog) -> None:
    result = link_chunk("A Cisterna Romana I foi escavada.",
                        catalog=catalog, project_id="proj-lnk")
    assert link_ids(result) == [element_of(catalog, "Cisterna Romana I")]
    plain = link_chunk("A Cisterna Romana foi escavada.",
                       catalog=catalog, project_id="proj-lnk")
    assert link_ids(plain) == [element_of(catalog, "Cisterna Romana")]


# --------------------------------------------------------------------------- #
# §12 — location disambiguation
# --------------------------------------------------------------------------- #
def test_duplicate_name_without_context_is_ambiguous(catalog) -> None:
    result = link_chunk("A Porta Principal foi restaurada.",
                        catalog=catalog, project_id="proj-lnk")
    assert result.links == ()
    assert [m.outcome for m in result.mentions] == [MentionOutcome.AMBIGUOUS_DUPLICATE_NAME]


def test_storey_and_space_evidence_disambiguate(catalog) -> None:
    by_storey = link_chunk("No Piso 0 a Porta Principal foi restaurada.",
                           catalog=catalog, project_id="proj-lnk")
    assert link_ids(by_storey) == [element_of(catalog, "Porta Principal", "Piso 0")]
    assert by_storey.links[0].method.value == "exact_name_location"
    assert by_storey.links[0].location_levels_used == ("storey",)

    by_space = link_chunk("Na Sala Capitular a Porta Principal foi restaurada.",
                          catalog=catalog, project_id="proj-lnk")
    assert by_space.links[0].location_levels_used == ("space",)
    assert by_space.links[0].method.value == "exact_name_location"


def test_insufficient_location_evidence_stays_ambiguous(catalog) -> None:
    """Piso 1 still leaves two candidates (one of them space-qualified)."""
    result = link_chunk("No Piso 1 a Porta Principal foi restaurada.",
                        catalog=catalog, project_id="proj-lnk")
    assert result.links == ()
    assert [m.outcome for m in result.mentions] == [MentionOutcome.AMBIGUOUS_DUPLICATE_NAME]


def test_conflicting_location_evidence_is_rejected(catalog) -> None:
    result = link_chunk("Entre o Piso 0 e o Piso 1 a Porta Principal foi vista.",
                        catalog=catalog, project_id="proj-lnk")
    assert result.links == ()
    assert [m.outcome for m in result.mentions] == [
        MentionOutcome.AMBIGUOUS_LOCATION_CONFLICT
    ]


def test_location_never_creates_a_link_by_itself(catalog) -> None:
    result = link_chunk("No Piso 0 decorreram trabalhos gerais.",
                        catalog=catalog, project_id="proj-lnk")
    assert result.links == ()


def test_location_names_colliding_under_normalisation_fail_closed(catalog_records) -> None:
    """§9/§12 — `Piso 1` and `Piso -1` both normalise to `piso 1`.

    The evidence then cannot separate them, so it must not filter: two distinct
    raw values are a conflict, never a silent pick.
    """
    records = json.loads(json.dumps(catalog_records))
    doors = [r for r in records if r["name"] == "Porta Principal"]
    doors[0]["location"]["storey"] = {"name": "Piso 1"}
    doors[1]["location"]["storey"] = {"name": "Piso -1"}
    doors[2]["location"]["storey"] = {"name": "Piso 2"}
    doors[2]["location"].pop("space", None)
    colliding = build_catalog(records, project_id="proj-lnk")
    result = link_chunk("No Piso 1 a Porta Principal foi restaurada.",
                        catalog=colliding, project_id="proj-lnk")
    assert result.links == ()
    assert [m.outcome for m in result.mentions] == [
        MentionOutcome.AMBIGUOUS_LOCATION_CONFLICT
    ]


# --------------------------------------------------------------------------- #
# §14/§15 — fuzzy metric, threshold, margin
# --------------------------------------------------------------------------- #
def test_osa_distance_literals() -> None:
    assert osa_distance("abc", "abc", 5) == 0
    assert osa_distance("abc", "abd", 5) == 1          # substitution
    assert osa_distance("abc", "ab", 5) == 1           # deletion
    assert osa_distance("ab", "abc", 5) == 1           # insertion
    assert osa_distance("ab", "ba", 5) == 1            # transposition
    assert osa_distance("abcd", "badc", 5) == 2
    assert osa_distance("abc", "xyz", 1) == 2          # early exit above the bound


def test_similarity_literals() -> None:
    assert similarity("abc", "abc") == 1.0
    assert similarity("", "abc") == 0.0
    assert similarity("muralha nrote", "muralha norte") == pytest.approx(1 - 1 / 13)
    assert similarity("cistema romana", "cisterna romana") == pytest.approx(1 - 2 / 15)


def test_fuzzy_resolves_a_unique_ocr_error(catalog) -> None:
    typo = link_chunk("A Cistema Romana foi escavada em 1998.",
                      catalog=catalog, project_id="proj-lnk")
    assert link_ids(typo) == [element_of(catalog, "Cisterna Romana")]
    link = typo.links[0]
    assert link.method.value == "fuzzy_name"
    assert link.score == pytest.approx(1 - 2 / 15)
    assert link.runner_up_score is not None
    assert link.score - link.runner_up_score >= FUZZY_MIN_MARGIN

    transposed = link_chunk("A Muralha Nrote apresenta colonizacao.",
                            catalog=catalog, project_id="proj-lnk")
    assert link_ids(transposed) == [element_of(catalog, "Muralha Norte")]


def test_below_threshold_stays_unresolved(catalog) -> None:
    result = link_chunk("A Muralha Nrt foi inspeccionada.",
                        catalog=catalog, project_id="proj-lnk")
    assert result.links == ()
    assert MentionOutcome.UNRESOLVED_BELOW_THRESHOLD in {m.outcome for m in result.mentions}


def test_near_tie_and_exact_tie_are_rejected_never_broken_by_id(catalog) -> None:
    near = link_chunk("A Cistema Romana I foi escavada.",
                      catalog=catalog, project_id="proj-lnk")
    assert near.links == ()
    assert MentionOutcome.AMBIGUOUS_FUZZY_MARGIN in {m.outcome for m in near.mentions}

    tie = link_chunk("A Camara 10l foi limpa.", catalog=catalog, project_id="proj-lnk")
    assert tie.links == ()
    assert MentionOutcome.AMBIGUOUS_FUZZY_MARGIN in {m.outcome for m in tie.mentions}


def test_fuzzy_candidate_bound_makes_the_mention_unresolved(catalog, monkeypatch) -> None:
    import ingestion.entity_linking as module

    monkeypatch.setattr(module, "MAX_FUZZY_CANDIDATES_PER_MENTION", 0)
    result = link_chunk("A Cistema Romana foi escavada.",
                        catalog=catalog, project_id="proj-lnk")
    assert result.links == ()
    assert MentionOutcome.UNRESOLVED_CANDIDATE_BOUND in {m.outcome for m in result.mentions}


# --------------------------------------------------------------------------- #
# §7/§17/§18 — isolation, ordering, provenance
# --------------------------------------------------------------------------- #
def test_cross_project_chunk_is_refused_before_matching(catalog) -> None:
    with pytest.raises(LinkInputError):
        link_chunk("A Muralha Norte pertence a outro projeto.",
                   catalog=catalog, project_id="proj-other")


def test_multiple_elements_and_repeated_mentions(catalog) -> None:
    multi = link_chunk("A Muralha Norte e a Torre Nordeste foram vistas.",
                       catalog=catalog, project_id="proj-lnk")
    assert link_ids(multi) == [
        element_of(catalog, "Muralha Norte"), element_of(catalog, "Torre Nordeste")
    ]
    starts = [link.mentions[0].start for link in multi.links]
    assert starts == sorted(starts)

    repeated = link_chunk("A Muralha Norte cedeu. A Muralha Norte foi consolidada.",
                          catalog=catalog, project_id="proj-lnk")
    assert len(repeated.links) == 1
    assert len(repeated.links[0].mentions) == 2
    spans = [(m.start, m.end) for m in repeated.links[0].mentions]
    assert spans == sorted(spans)


def test_mention_text_equals_the_original_slice(catalog) -> None:
    text = "A «Muralha Norte» — inspeccao e a Torre Nordeste."
    result = link_chunk(text, catalog=catalog, project_id="proj-lnk")
    assert result.links
    for link in result.links:
        for mention in link.mentions:
            assert text[mention.start:mention.end] == mention.text
            assert mention.start < mention.end


def test_provenance_records_class_and_material_evidence(catalog) -> None:
    result = link_chunk("A Muralha Norte de granito, uma parede historica.",
                        catalog=catalog, project_id="proj-lnk")
    link = result.links[0]
    assert link.ifc_class == "IfcWall"
    assert link.material_names_mentioned == ("Granito",)
    # §11/§13 — no PT/EN alias table in v1: "parede" is NOT read as IfcWall.
    # The flag means the class token itself occurs in the text.
    assert link.ifc_class_mentioned is False
    literal = link_chunk("A Muralha Norte (IfcWall) foi vista.",
                         catalog=catalog, project_id="proj-lnk")
    assert literal.links[0].ifc_class_mentioned is True


def test_class_and_material_never_create_a_link(catalog) -> None:
    """§13 — evidence only, never identity."""
    result = link_chunk("Uma parede de granito foi consolidada.",
                        catalog=catalog, project_id="proj-lnk")
    assert result.links == ()


def test_page_number_only_for_single_page_chunks(catalog) -> None:
    single = link_chunk("A Muralha Norte cedeu.", catalog=catalog,
                        project_id="proj-lnk", page_span=(3, 3))
    assert single.links[0].mentions[0].page_number == 3
    spanning = link_chunk("A Muralha Norte cedeu.", catalog=catalog,
                          project_id="proj-lnk", page_span=(3, 4))
    assert spanning.links[0].mentions[0].page_number is None
    assert spanning.links[0].mentions[0].region_index is None


def test_region_index_only_when_exactly_one_region_on_that_page(catalog) -> None:
    one = link_chunk("A Muralha Norte cedeu.", catalog=catalog, project_id="proj-lnk",
                     page_span=(2, 2), page_regions=({"page_number": 2, "region_index": 7},))
    assert one.links[0].mentions[0].region_index == 7
    many = link_chunk("A Muralha Norte cedeu.", catalog=catalog, project_id="proj-lnk",
                      page_span=(2, 2),
                      page_regions=({"page_number": 2, "region_index": 7},
                                    {"page_number": 2, "region_index": 8}))
    assert many.links[0].mentions[0].region_index is None


def test_links_per_chunk_bound_is_typed(catalog, monkeypatch) -> None:
    import ingestion.entity_linking as module
    from ingestion.entity_linking import LinkBoundsError

    monkeypatch.setattr(module, "MAX_LINKS_PER_CHUNK", 1)
    with pytest.raises(LinkBoundsError):
        link_chunk("A Muralha Norte e a Torre Nordeste foram vistas.",
                   catalog=catalog, project_id="proj-lnk")


def test_linking_is_deterministic(catalog) -> None:
    text = "A Muralha Norte, a Torre Nordeste e a Cistema Romana."
    first = link_chunk(text, catalog=catalog, project_id="proj-lnk")
    second = link_chunk(text, catalog=catalog, project_id="proj-lnk")
    assert [
        (link.element_id, link.method, link.score, tuple((m.start, m.end) for m in link.mentions))
        for link in first.links
    ] == [
        (link.element_id, link.method, link.score, tuple((m.start, m.end) for m in link.mentions))
        for link in second.links
    ]


# --------------------------------------------------------------------------- #
# §20/§26 — purity, privacy, import safety
# --------------------------------------------------------------------------- #
def test_config_fingerprint_binds_every_output_affecting_setting() -> None:
    import ingestion.entity_linking as module

    assert isinstance(LINK_CONFIG_FINGERPRINT, str) and LINK_CONFIG_FINGERPRINT
    source = Path(module.__file__).read_text(encoding="utf-8")
    body = source[source.index("LINK_CONFIG_FINGERPRINT"):]
    body = body[: body.index("\n)")]
    for name in ("LINKER_VERSION", "LINKER_NORMALIZATION_VERSION", "FUZZY_METRIC_VERSION",
                 "FUZZY_MIN_SCORE", "FUZZY_MIN_MARGIN", "MIN_ELIGIBLE_NAME_CHARS",
                 "MAX_NAME_TOKENS", "MAX_FUZZY_CANDIDATES_PER_MENTION", "STOP_NAMES"):
        assert name in body, name


def test_module_imports_nothing_forbidden() -> None:
    """§20/§26 — no model, no client, no network, no subprocess."""
    import ingestion.entity_linking as module

    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.add(node.module.split(".")[0])
    forbidden = {
        "opensearchpy", "torch", "transformers", "openai", "anthropic", "requests",
        "httpx", "socket", "subprocess", "urllib", "paddleocr", "neo4j",
    }
    assert roots.isdisjoint(forbidden), roots & forbidden
    assert "retrieval" not in roots      # layering: no runtime retrieval import
    assert "api" not in roots


def test_import_opens_no_socket(monkeypatch) -> None:
    import socket as socket_module

    def boom(*args: object, **kwargs: object):
        raise AssertionError("the linker attempted network access")

    monkeypatch.setattr(socket_module.socket, "connect", boom)
    monkeypatch.setattr(socket_module, "create_connection", boom)
    import importlib

    import ingestion.entity_linking as module

    assert importlib.import_module(module.__name__) is module


def test_errors_never_carry_chunk_text(catalog) -> None:
    secret = "TEXTO CONFIDENCIAL DO RELATORIO"
    try:
        link_chunk(f"{secret} — Muralha Norte", catalog=catalog, project_id="proj-other")
    except LinkInputError as exc:
        assert secret not in str(exc)
    else:  # pragma: no cover - the call must raise
        raise AssertionError("expected LinkInputError")
