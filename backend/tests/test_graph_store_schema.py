"""HBIM-082 §23/§27/§40 — labels, relationship types, constraints and templates.

Closure of the label and type sets, the constraint and index statements
`ensure_schema` issues, and the property the §109 correction turns on: every
write template keys on an *occurrence* identity and pins the node generation.
Pure and offline.
"""

from __future__ import annotations

import ast
import pathlib
import re

import pytest
from graph_store import schema as S


def test_schema_versions_are_the_documented_pair() -> None:
    assert S.KG_SCHEMA_VERSION_V1 == "hbim-082-kg-v1"
    assert S.KG_SCHEMA_VERSION == "hbim-082-kg-v2"
    assert S.KG_SCHEMA_VERSION != S.KG_SCHEMA_VERSION_V1


def test_label_set_is_closed_and_canonical() -> None:
    assert S.CANONICAL_LABEL == "CanonicalNode"
    assert S.PROJECT_ROOT_LABEL == "ProjectRoot"
    for label in S.LABEL_BY_KIND.values():
        assert label.isidentifier() and label[0].isupper()
    assert len(set(S.LABEL_BY_KIND.values())) == len(S.LABEL_BY_KIND)


def test_relationship_types_are_closed_and_uppercase() -> None:
    assert len(S.RELATIONSHIP_TYPES) == 21
    for rel_type in S.RELATIONSHIP_TYPES.values():
        assert rel_type.isidentifier() and rel_type == rel_type.upper()
    assert len(set(S.RELATIONSHIP_TYPES.values())) == len(S.RELATIONSHIP_TYPES)


def test_no_generic_catch_all_relationship_type() -> None:
    """§95 — a generic `CONNECTED_TO` would erase the predicate."""
    assert "CONNECTED_TO" not in set(S.RELATIONSHIP_TYPES.values())


def test_reserved_labels_are_not_reused_as_domain_labels() -> None:
    assert S.PROJECT_ROOT_LABEL not in set(S.LABEL_BY_KIND.values())
    assert S.CANONICAL_LABEL not in set(S.LABEL_BY_KIND.values())


# --------------------------------------------------------------------------- #
# constraints and indexes (§27)
# --------------------------------------------------------------------------- #
ALL_SCHEMA = tuple(S.CONSTRAINTS) + tuple(S.RELATIONSHIP_CONSTRAINTS) + tuple(S.INDEXES)
STATEMENTS = tuple(statement for _name, statement in ALL_SCHEMA)


def test_every_schema_entry_is_a_named_statement_pair() -> None:
    for name, statement in ALL_SCHEMA:
        assert isinstance(name, str) and isinstance(statement, str)
        assert name in statement


def test_node_constraints_are_nuc1_only() -> None:
    joined = "\n".join(s for _n, s in S.CONSTRAINTS)
    assert "node_instance_id" in joined
    assert re.search(r"\(n\.node_id\)\s+IS\s+UNIQUE", joined) is None
    assert "IS NODE KEY" not in joined.upper()
    assert "IS NOT NULL" not in joined.upper()


def test_one_relationship_uniqueness_constraint_per_type() -> None:
    assert len(S.RELATIONSHIP_CONSTRAINTS) == len(S.RELATIONSHIP_TYPES) == 21
    for _name, statement in S.RELATIONSHIP_CONSTRAINTS:
        assert "relationship_instance_id" in statement
        assert "IS UNIQUE" in statement
        assert "IF NOT EXISTS" in statement


def test_constraint_names_are_unique_and_prefixed() -> None:
    names = [name for name, _s in ALL_SCHEMA]
    assert len(names) == len(set(names))
    assert all(name.startswith("hbim082_") for name in names)
    parsed = re.findall(r"(?:CONSTRAINT|INDEX)\s+(\S+)\s+IF NOT EXISTS",
                        "\n".join(STATEMENTS))
    assert sorted(parsed) == sorted(names)


def test_every_schema_statement_is_idempotent() -> None:
    for statement in STATEMENTS:
        assert "IF NOT EXISTS" in statement


def test_no_enterprise_only_feature_is_requested() -> None:
    joined = " ".join(STATEMENTS).upper()
    for forbidden in ("NODE KEY", "REQUIRE EXISTENCE", "CREATE DATABASE", "ALIAS"):
        assert forbidden not in joined


# --------------------------------------------------------------------------- #
# write templates (§40 / §109)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("kind", list(S.LABEL_BY_KIND))
def test_node_template_merges_on_the_occurrence_identity(kind: object) -> None:
    template = S.node_template(kind)  # type: ignore[arg-type]
    assert "MERGE (n:CanonicalNode {node_instance_id: row.node_instance_id})" in template
    assert "MERGE (n:CanonicalNode {node_id:" not in template


@pytest.mark.parametrize("predicate", list(S.RELATIONSHIP_TYPES))
def test_edge_template_merges_on_the_occurrence_identity(predicate: object) -> None:
    template = S.edge_template(predicate)  # type: ignore[arg-type]
    assert "{relationship_instance_id: row.relationship_instance_id}" in template
    assert "{edge_id: row.edge_id}" not in template


@pytest.mark.parametrize("predicate", list(S.RELATIONSHIP_TYPES))
def test_edge_template_matches_endpoints_by_occurrence_and_pins_the_generation(
    predicate: object,
) -> None:
    """The write side of the §41-check-15 contract the read side must mirror."""
    template = S.edge_template(predicate)  # type: ignore[arg-type]
    assert "{node_instance_id: row.source_node_instance_id}" in template
    assert "{node_instance_id: row.target_node_instance_id}" in template
    assert template.count("a.node_revision_id = $node_revision_id") == 1
    assert template.count("b.node_revision_id = $node_revision_id") == 1
    assert template.count("a.project_id = $project_id") == 1
    assert template.count("b.project_id = $project_id") == 1


@pytest.mark.parametrize("predicate", list(S.RELATIONSHIP_TYPES))
def test_edge_template_sets_one_static_relationship_type(predicate: object) -> None:
    template = S.edge_template(predicate)  # type: ignore[arg-type]
    assert S.RELATIONSHIP_TYPES[predicate] in template  # type: ignore[index]
    assert "$rel_type" not in template and "$type" not in template


def test_templates_enumerate_properties_rather_than_splatting() -> None:
    """`SET n += row` would let an unknown key through unnoticed."""
    for template in list(S.NODE_TEMPLATES.values()) + list(S.EDGE_TEMPLATES.values()):
        assert " += row" not in template


def test_every_template_carries_no_write_clause_beyond_merge_and_set() -> None:
    for template in list(S.NODE_TEMPLATES.values()) + list(S.EDGE_TEMPLATES.values()):
        upper = template.upper()
        for forbidden in ("DELETE", "DETACH", "DROP", "CALL DB.", "APOC.", "LOAD CSV"):
            assert forbidden not in upper


def test_no_template_interpolates_a_row_value_into_cypher_text() -> None:
    """AST proof: every template is built from literals and module constants."""
    source = pathlib.Path(S.__file__).read_text()
    tree = ast.parse(source)
    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            for value in node.values:
                if isinstance(value, ast.FormattedValue):
                    expr = value.value
                    ok = (
                        isinstance(expr, ast.Name)
                        or (isinstance(expr, ast.Attribute))
                        or (isinstance(expr, ast.Call)
                            and isinstance(expr.func, ast.Attribute))
                    )
                    if not ok:
                        offenders.append(ast.dump(expr)[:60])
    assert not offenders, offenders


def test_edge_and_node_template_registries_are_closed() -> None:
    assert set(S.EDGE_TEMPLATES) == set(S.RELATIONSHIP_TYPES)
    assert set(S.NODE_TEMPLATES) == set(S.LABEL_BY_KIND)
    with pytest.raises(S.SchemaError):
        S.edge_template("not-a-predicate")  # type: ignore[arg-type]
    with pytest.raises(S.SchemaError):
        S.node_template("not-a-kind")  # type: ignore[arg-type]


def test_property_allowlists_include_the_occurrence_columns() -> None:
    for allowlist in (S.NATIVE_EDGE_PROPERTIES, S.DERIVED_EDGE_PROPERTIES):
        assert "relationship_instance_id" in allowlist
        assert "source_node_instance_id" in allowlist
        assert "target_node_instance_id" in allowlist
    assert "node_instance_id" in S.NODE_PROPERTIES
    assert "node_revision_id" in S.NODE_PROPERTIES
