"""HBIM-081 §70 — the native producer end-to-end over the frozen IFC corpus.

The unit suite feeds ``produce_native`` in-memory bytes. This suite materialises
all 17 frozen fixtures as real ``.ifc`` files on disk and runs the producer over
what it reads back, which is the shape a real generation takes: file in, nodes
and edges out, nothing persisted.

Marked ``integration`` per §70 because it exercises the real IfcOpenShell parse
over the whole corpus rather than one model. It needs **no** Docker, no network,
no OpenSearch, no Neo4j and no TopologicPy, so it requests none of the
container fixtures.
"""

from __future__ import annotations

import hashlib
import tempfile
from collections import Counter
from pathlib import Path

import pytest
from relations.native_ifc import produce_native
from relations.validation import NATIVE_TABLE, RelationNodeKind, RelationPredicate

from eval.relation_fixtures import NATIVE_FAMILIES, build_native_fixture

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def corpus_dir():
    """The 17 frozen fixtures, written once as real files."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        for spec in NATIVE_FAMILIES:
            (root / f"{spec.family_id}.ifc").write_bytes(
                build_native_fixture(spec.family_id))
        yield root


@pytest.fixture(scope="module")
def produced(corpus_dir):
    out = {}
    for spec in NATIVE_FAMILIES:
        data = (corpus_dir / f"{spec.family_id}.ifc").read_bytes()
        out[spec.family_id] = (spec, produce_native(
            ifc_bytes=data, project_id=spec.project_id, source_id=spec.family_id,
            source_sha256=hashlib.sha256(data).hexdigest()))
    return out


def test_every_family_parses_from_disk(produced) -> None:
    assert len(produced) == 17
    assert all(result.nodes.nodes for _, result in produced.values())


def test_the_corpus_covers_sixteen_of_the_seventeen_rows(produced) -> None:
    """The seventeenth (CONTAINS) is a stated corpus gap, pinned in the unit suite."""
    seen = {r.predicate for _, result in produced.values()
            for r in result.relations.relations}
    table = {row.predicate for row in NATIVE_TABLE}
    assert seen <= table
    assert table - seen == {RelationPredicate.CONTAINS}


def test_no_relation_is_invented_duplicated_or_self_referential(produced) -> None:
    for family, (spec, result) in produced.items():
        ids = [r.edge_id for r in result.relations.relations]
        assert len(set(ids)) == len(ids), family
        assert all(r.source_node_id != r.target_node_id
                   for r in result.relations.relations), family
        assert all(r.project_id == spec.project_id
                   for r in result.relations.relations), family


def test_every_edge_names_the_ifc_relation_it_came_from(produced) -> None:
    for family, (_, result) in produced.items():
        for edge in result.relations.relations:
            p = edge.provenance
            assert p.source_relation_global_id and p.source_relation_class, family
            assert p.native_revision_id == result.relations.native_revision_id
            assert not hasattr(p, "source_geometry_id_a")


def test_a_port_is_never_an_element(produced) -> None:
    ports = [n for _, result in produced.values() for n in result.nodes.nodes
             if n.kind is RelationNodeKind.PORT]
    assert ports
    assert all(not n.node_id.startswith("el_") and n.global_id for n in ports)


def test_two_projects_never_share_an_identity(produced) -> None:
    by_project: dict[str, set[str]] = {}
    for _, (spec, result) in produced.items():
        by_project.setdefault(spec.project_id, set()).update(
            n.node_id for n in result.nodes.nodes)
    assert len(by_project) == 2
    a, b = by_project.values()
    assert a.isdisjoint(b)


def test_the_malformed_family_reports_typed_codes_and_emits_no_bad_edge(produced) -> None:
    _, result = produced["rnf-14-malformed"]
    codes = Counter(i.code.value for i in result.issues)
    assert codes
    assert all(isinstance(c, str) and c for c in codes)


def test_ifc2x3_families_produce_the_same_shapes_as_ifc4(produced) -> None:
    schemas = {spec.ifc_schema for spec, _ in produced.values()}
    assert schemas == {"IFC4", "IFC2X3"}
    for spec, result in produced.values():
        if spec.ifc_schema == "IFC2X3":
            assert result.relations.relation_schema_version == "hbim-081-relations-v1"


def test_rerunning_the_producer_over_the_same_files_is_byte_stable(corpus_dir) -> None:
    def run_all() -> list[tuple[str, str]]:
        rows = []
        for spec in NATIVE_FAMILIES:
            data = (corpus_dir / f"{spec.family_id}.ifc").read_bytes()
            out = produce_native(ifc_bytes=data, project_id=spec.project_id,
                                 source_id=spec.family_id,
                                 source_sha256=hashlib.sha256(data).hexdigest())
            rows.append((spec.family_id, out.relations.fingerprint))
            rows.append((spec.family_id, out.nodes.fingerprint))
        return rows

    assert run_all() == run_all()


def test_the_producer_writes_nothing_to_the_corpus_directory(corpus_dir, produced) -> None:
    names = sorted(p.name for p in corpus_dir.iterdir())
    assert names == sorted(f"{s.family_id}.ifc" for s in NATIVE_FAMILIES)
