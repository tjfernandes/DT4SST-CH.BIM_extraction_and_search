"""HBIM-020 — offline validation of the four static OpenSearch mappings.

The mappings are *data* (versioned JSON), not code: these tests read them
directly with ``json`` + ``pathlib`` (there is no loader — the first consumer is
HBIM-021). Field coverage is driven by the real Pydantic ``model_fields`` of the
canonical records, never a hand-maintained list, so schema drift fails a test.

No network, no Docker, no OpenSearch: pure structural assertions on the JSON.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from canonical import schema as s

# The mappings live beside the canonical package; anchor to it so the location is
# independent of the current working directory. Reading the JSON is not a loader.
MAPPINGS_DIR = Path(s.__file__).resolve().parent / "mappings"

# record_type -> (filename, canonical model name). Models are referenced by NAME
# and resolved with getattr(s, ...) at call time — never captured as class objects
# at import time. The import-safety suite reloads canonical.schema in-process,
# which rebinds the model classes; an identity check (``is``) or a captured class
# would then break under some test orders. model_fields (the names) is stable
# across reload, so resolving fresh is correct and order-independent.
FILES: dict[str, tuple[str, str]] = {
    "element": ("elements_v1.json", "ElementRecord"),
    "property_fact": ("property_facts_v1.json", "PropertyFact"),
    "classification_fact": ("classification_facts_v1.json", "ClassificationFact"),
    "document": ("documents_v1.json", "DocumentRef"),
}
FILENAMES = {fn for fn, _ in FILES.values()}

# PropertyFact.value (the polymorphic canonical object) is never mapped; the doc
# indexed by HBIM-022 carries this typed, disjoint projection instead. The
# transformation the mapping encodes:
#     value
#       -> value_type      (keyword, always)
#       -> value_is_null   (boolean, always)
#       -> value_text      (text + keyword, when value_type == "text")
#       -> value_integer   (long,   when value_type == "int")
#       -> value_number    (double, when value_type == "float")
#       -> value_boolean   (boolean, when value_type == "bool")
PROPERTY_VALUE_PROJECTION: tuple[str, ...] = (
    "value_type",
    "value_is_null",
    "value_text",
    "value_integer",
    "value_number",
    "value_boolean",
)

# Every object/nested node whose declared sub-properties must equal a real
# model's ``model_fields`` (coverage tied to the models, recursively). Model names
# are resolved with getattr(s, ...) at call time (reload-robust, as above).
SUBOBJECT_MODELS: dict[str, dict[str, str]] = {
    "elements_v1.json": {
        "materials": "MaterialRef",
        "location": "SpatialLocation",
        "location.site": "SpatialRef",
        "location.building": "SpatialRef",
        "location.storey": "SpatialRef",
        "location.space": "SpatialRef",
        "location.parent_element": "SpatialRef",
        "metrics": "Metrics",
        "source": "SourceRef",
    },
    "property_facts_v1.json": {},
    "classification_facts_v1.json": {"source": "SourceRef"},
    "documents_v1.json": {"source": "SourceRef"},
}

# Deterministic identity fields (keyword, case-sensitive, never lowercased).
ID_FIELDS: dict[str, tuple[str, ...]] = {
    "elements_v1.json": ("element_id", "project_id", "global_id"),
    "property_facts_v1.json": ("fact_id", "project_id", "element_id"),
    "classification_facts_v1.json": ("classification_id", "project_id", "element_id"),
    "documents_v1.json": ("document_id", "project_id"),
}

# coerce:false is mandatory on these numeric paths (reject silent string→number).
COERCE_FALSE_PATHS: dict[str, tuple[str, ...]] = {
    "elements_v1.json": (
        "materials.ordinal",
        "metrics.area",
        "metrics.volume",
        "metrics.height",
        "metrics.thickness",
    ),
    "property_facts_v1.json": ("value_integer", "value_number"),
    "classification_facts_v1.json": (),
    "documents_v1.json": (),
}

# Names that must never appear as mapping fields (superseded ROADMAP / legacy).
FORBIDDEN_FIELD_NAMES = {
    "semantic_text",
    "semantic_embedding",
    "embedding_qwen3",
    "classification_codes",
    "classification_text",
    "element_text",
    "spatial_hierarchy",
    "project_name",
    "material",  # singular; the real field is plural "materials"
    "quantities",
    "geometry",
    "relations_summary",
    "evidence_refs",
    "space_id",
    "space_name",
    "value",  # the polymorphic canonical object is projected away (§5)
}

VECTOR_TOKENS = ("knn_vector", "semantic_embedding", "embedding_qwen3", "dimension", "knn")


# --------------------------------------------------------------------------- #
# Helpers (pure JSON introspection)
# --------------------------------------------------------------------------- #
def _load(filename: str) -> dict:
    return json.loads((MAPPINGS_DIR / filename).read_text(encoding="utf-8"))


def _node_at(mapping: dict, dotted: str) -> dict:
    """Navigate ``properties`` by a dotted field path (``""`` -> root)."""
    node = mapping
    if dotted:
        for seg in dotted.split("."):
            node = node["properties"][seg]
    return node


def _field_names(node: dict) -> set[str]:
    return set(node.get("properties", {}))


def _iter_object_nodes(mapping: dict) -> Iterator[tuple[str, dict]]:
    """Yield ``(path, node)`` for every object/nested node below the root."""
    stack: list[tuple[str, dict]] = [("", mapping)]
    while stack:
        path, node = stack.pop()
        for name, child in node.get("properties", {}).items():
            child_path = f"{path}.{name}" if path else name
            is_container = isinstance(child, dict) and (
                "properties" in child or child.get("type") in ("object", "nested")
            )
            if is_container:
                yield child_path, child
                stack.append((child_path, child))


def _iter_all_dicts(obj: object) -> Iterator[dict]:
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from _iter_all_dicts(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _iter_all_dicts(v)


def _all_field_names(mapping: dict) -> set[str]:
    """Every declared field name (keys inside any ``properties`` block)."""
    names: set[str] = set()
    for node in _iter_all_dicts(mapping):
        props = node.get("properties")
        if isinstance(props, dict):
            names.update(props)
    return names


ALL_FILENAMES = list(FILENAMES)


# --------------------------------------------------------------------------- #
# 1–2. File set: exactly four, no chunks, no loader/__init__.py
# --------------------------------------------------------------------------- #
def test_mapping_file_set_is_exactly_the_four_v1_plus_elements_v2() -> None:
    # HBIM-031 added the single vector mapping `elements_v2.json` (selected
    # dimension; see eval/baselines/dimension_decision.json). Every assertion
    # in this module stays v1-scoped; v2 has its own dedicated suite
    # (test_elements_v2_mapping.py).
    json_files = {p.name for p in MAPPINGS_DIR.glob("*.json")}
    assert json_files == FILENAMES | {"elements_v2.json"}


def test_no_chunks_no_loader_no_python_modules() -> None:
    assert not (MAPPINGS_DIR / "chunks_v1.json").exists()  # deferred to HBIM-070
    assert not (MAPPINGS_DIR / "__init__.py").exists()  # no importable subpackage
    assert list(MAPPINGS_DIR.glob("*.py")) == []  # no loader in HBIM-020


# --------------------------------------------------------------------------- #
# 3–6. JSON validity, top-level shape, _meta, top-level strictness
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("filename", ALL_FILENAMES)
def test_valid_json_object(filename: str) -> None:
    assert isinstance(_load(filename), dict)


@pytest.mark.parametrize("filename", ALL_FILENAMES)
def test_top_level_keys_and_strictness(filename: str) -> None:
    m = _load(filename)
    assert set(m) == {"_meta", "dynamic", "_source", "properties"}  # mappings-only
    assert m["dynamic"] == "strict"
    assert m["_source"] == {"enabled": True}
    assert isinstance(m["properties"], dict) and m["properties"]


@pytest.mark.parametrize("record_type,pair", list(FILES.items()))
def test_meta_is_exact(record_type: str, pair: tuple[str, str]) -> None:
    filename, _ = pair
    meta = _load(filename)["_meta"]
    assert set(meta) == {"canonical_schema_versions", "mapping_version", "record_type", "created_by"}
    assert meta["canonical_schema_versions"] == ["1.0"]
    assert meta["mapping_version"] == "1"
    assert meta["created_by"] == "HBIM-020"
    assert meta["record_type"] == record_type
    # No timestamp-like drift keys leaked into _meta.
    for key in meta:
        assert not any(tok in key.lower() for tok in ("time", "date", "created_at", "stamp"))


# --------------------------------------------------------------------------- #
# 7–9. Recursive strictness; no dynamic:true; no object/nested without properties
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("filename", ALL_FILENAMES)
def test_every_object_or_nested_is_strict_with_properties(filename: str) -> None:
    m = _load(filename)
    seen = 0
    for path, node in _iter_object_nodes(m):
        assert node.get("dynamic") == "strict", (filename, path, "missing dynamic:strict")
        assert isinstance(node.get("properties"), dict) and node["properties"], (filename, path)
        assert node.get("type") in ("object", "nested"), (filename, path)
        seen += 1
    # Exact counts: elements = materials + location + 5 spatial refs + metrics +
    # source; classification/documents = source only; property_facts = none
    # (its value projection is flat, typed, disjoint scalars).
    expected_counts = {
        "elements_v1.json": 9,
        "property_facts_v1.json": 0,
        "classification_facts_v1.json": 1,
        "documents_v1.json": 1,
    }
    assert seen == expected_counts[filename], (filename, seen)


@pytest.mark.parametrize("filename", ALL_FILENAMES)
def test_no_dynamic_true_anywhere(filename: str) -> None:
    m = _load(filename)
    dynamics = [d.get("dynamic") for d in _iter_all_dicts(m) if "dynamic" in d]
    assert dynamics, filename
    assert all(v == "strict" for v in dynamics), (filename, dynamics)


# --------------------------------------------------------------------------- #
# 10. No vectors / embedding fields at any level
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("filename", ALL_FILENAMES)
def test_no_vector_or_embedding_fields(filename: str) -> None:
    m = _load(filename)
    for node in _iter_all_dicts(m):
        assert node.get("type") != "knn_vector"
        assert "dimension" not in node
    names = _all_field_names(m)
    for tok in ("semantic_embedding", "embedding_qwen3"):
        assert tok not in names
    raw = (MAPPINGS_DIR / filename).read_text(encoding="utf-8")
    for tok in VECTOR_TOKENS:
        assert tok not in raw, (filename, tok)


# --------------------------------------------------------------------------- #
# 11–12. IDs keyword (no normalizer); checksums indexable keyword
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("filename", ALL_FILENAMES)
def test_ids_are_keyword_without_normalizer(filename: str) -> None:
    m = _load(filename)
    for field in ID_FIELDS[filename]:
        node = m["properties"][field]
        assert node == {"type": "keyword"}, (filename, field)  # keyword, no normalizer
    # No normalizer anywhere (the legacy lowercase-id anti-pattern is forbidden).
    for node in _iter_all_dicts(m):
        assert "normalizer" not in node, filename
    # Spatial identity fields are keyword too.
    if filename == "elements_v1.json":
        for ref in ("site", "building", "storey", "space", "parent_element"):
            sub = _node_at(m, f"location.{ref}")
            assert sub["properties"]["global_id"] == {"type": "keyword"}
            assert sub["properties"]["id"] == {"type": "keyword"}


@pytest.mark.parametrize("filename", ALL_FILENAMES)
def test_indexing_is_never_disabled(filename: str) -> None:
    # No field disables indexing anywhere (checksums stay exact-match searchable,
    # never index:false); the spec mandates checksum as an indexable keyword.
    for node in _iter_all_dicts(_load(filename)):
        assert node.get("index") is not False, filename


@pytest.mark.parametrize("filename", ALL_FILENAMES)
def test_checksums_are_indexable_keyword(filename: str) -> None:
    m = _load(filename)
    checksum_nodes = [
        node["properties"]["checksum"]
        for node in _iter_all_dicts(m)
        if isinstance(node.get("properties"), dict) and "checksum" in node["properties"]
    ]
    # PropertyFact has no SourceRef (its source is the scalar "pset"/"qto"), so it
    # carries no checksum; every other mapping exposes checksum as keyword.
    if filename == "property_facts_v1.json":
        assert checksum_nodes == []
    else:
        assert checksum_nodes
        for node in checksum_nodes:
            assert node == {"type": "keyword"}, filename


# --------------------------------------------------------------------------- #
# 13. materials is nested
# --------------------------------------------------------------------------- #
def test_materials_is_nested() -> None:
    m = _load("elements_v1.json")
    assert m["properties"]["materials"]["type"] == "nested"


# --------------------------------------------------------------------------- #
# 14. coerce:false on the mandated numeric paths
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("filename", ALL_FILENAMES)
def test_coerce_false_on_numeric_fields(filename: str) -> None:
    m = _load(filename)
    for path in COERCE_FALSE_PATHS[filename]:
        node = _node_at(m, path)
        assert node.get("type") in ("integer", "long", "double", "float"), (filename, path)
        assert node.get("coerce") is False, (filename, path)


# --------------------------------------------------------------------------- #
# 15. Field coverage == model_fields (root + nested), projection-aware
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("record_type,pair", list(FILES.items()))
def test_root_field_coverage_matches_model_fields(record_type: str, pair: tuple[str, str]) -> None:
    filename, model_name = pair
    model = getattr(s, model_name)  # current class (reload-robust)
    m = _load(filename)
    expected = set(model.model_fields)
    if record_type == "property_fact":
        # value (polymorphic canonical object) is projected into typed disjoint
        # scalar fields (§5); keyed on the stable record_type, never class identity.
        expected = (expected - {"value"}) | set(PROPERTY_VALUE_PROJECTION)
    actual = _field_names(m)
    assert actual == expected, (filename, "extra", actual - expected, "missing", expected - actual)


@pytest.mark.parametrize("filename", ALL_FILENAMES)
def test_nested_object_coverage_matches_submodels(filename: str) -> None:
    m = _load(filename)
    for path, model_name in SUBOBJECT_MODELS[filename].items():
        submodel = getattr(s, model_name)  # current class (reload-robust)
        node = _node_at(m, path)
        assert _field_names(node) == set(submodel.model_fields), (filename, path)


# --------------------------------------------------------------------------- #
# 16. PropertyFact projection is present, typed, disjoint; no polymorphic value
# --------------------------------------------------------------------------- #
def test_property_fact_value_projection_is_typed_and_disjoint() -> None:
    m = _load("property_facts_v1.json")
    props = m["properties"]
    assert "value" not in props  # the polymorphic canonical object is never mapped
    for name in PROPERTY_VALUE_PROJECTION:
        assert name in props, name
    assert props["value_type"] == {"type": "keyword"}
    assert props["value_is_null"] == {"type": "boolean"}
    assert props["value_boolean"] == {"type": "boolean"}
    assert props["value_integer"] == {"type": "long", "coerce": False}
    assert props["value_number"] == {"type": "double", "coerce": False}
    assert props["value_text"]["type"] == "text"
    assert props["value_text"]["fields"]["keyword"]["type"] == "keyword"


# --------------------------------------------------------------------------- #
# 17. No legacy / invented field names anywhere
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("filename", ALL_FILENAMES)
def test_no_legacy_or_invented_field_names(filename: str) -> None:
    m = _load(filename)
    offending = _all_field_names(m) & FORBIDDEN_FIELD_NAMES
    assert offending == set(), (filename, offending)


# --------------------------------------------------------------------------- #
# 18. Deterministic, byte-stable golden JSON (sorted keys, indent 2, no timestamps)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("filename", ALL_FILENAMES)
def test_json_is_byte_stable_canonical_form(filename: str) -> None:
    raw = (MAPPINGS_DIR / filename).read_text(encoding="utf-8")
    canonical = json.dumps(json.loads(raw), indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    assert raw == canonical  # committed golden == canonical re-serialisation
    lowered = raw.lower()
    for token in ("timestamp", "generated_at", "created_at", "datetime"):
        assert token not in lowered


# --------------------------------------------------------------------------- #
# Text multi-field contract (exact-match / aggregation on human strings)
# --------------------------------------------------------------------------- #
def test_element_text_fields_have_keyword_subfield() -> None:
    props = _load("elements_v1.json")["properties"]
    for field in ("name", "object_type", "semantic_label"):
        assert props[field]["type"] == "text"
        assert props[field]["fields"]["keyword"] == {"type": "keyword", "ignore_above": 256}
    assert props["description"] == {"type": "text"}  # free text, no keyword subfield
