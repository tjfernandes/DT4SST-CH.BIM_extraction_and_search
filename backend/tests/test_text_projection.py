"""HBIM-005B §18.2 — the versioned document-text projection.

Offline and pure: no network, no model, no OpenSearch, no settings.
"""

from __future__ import annotations

import pytest

from canonical.schema import ElementRecord
from eval.text_projection import (
    MAX_PROJECTED_CHARS,
    PROJECTED_FIELDS,
    PROJECTION_VERSION,
    project_element,
)

SOURCE = {"source_id": "src-synthetic", "ifc_schema": "IFC4"}


def make(**overrides: object) -> ElementRecord:
    payload: dict[str, object] = {
        "schema_version": "1.0",
        "element_id": "el_0000000000000000000000000000test",
        "project_id": "proj-test",
        "global_id": "3HTEST0000000000000001",
        "ifc_class": "IfcWall",
        "name": None,
        "description": None,
        "object_type": None,
        "predefined_type": None,
        "semantic_label": None,
        "materials": [],
        "location": {},
        "metrics": {},
        "source": SOURCE,
    }
    payload.update(overrides)
    return ElementRecord.model_validate(payload)


def ref(name: str) -> dict[str, object]:
    return {"name": name}


def test_version_is_pinned() -> None:
    assert PROJECTION_VERSION == "v1"


def test_fully_populated_record_golden() -> None:
    record = make(
        name="Parede mestra norte",
        description="Parede portante de silhares aparelhados.",
        object_type="Parede portante",
        predefined_type="SOLIDWALL",
        semantic_label="parede exterior portante",
        materials=[
            {"name": "granito", "ordinal": 0},
            {"name": "reboco de cal", "ordinal": 1},
        ],
        location={
            "site": ref("Convento de São Bento"),
            "building": ref("Claustro Norte"),
            "storey": ref("Piso Térreo"),
            "space": ref("Galeria Norte"),
        },
    )
    assert project_element(record) == (
        "IFC class: IfcWall\n"
        "Name: Parede mestra norte\n"
        "Description: Parede portante de silhares aparelhados.\n"
        "Object type: Parede portante\n"
        "Predefined type: SOLIDWALL\n"
        "Semantic label: parede exterior portante\n"
        "Materials: granito, reboco de cal\n"
        "Site: Convento de São Bento\n"
        "Building: Claustro Norte\n"
        "Storey: Piso Térreo\n"
        "Space: Galeria Norte"
    )


def test_minimal_record_omits_lines_entirely() -> None:
    # Absent values must vanish, never appear as "Name: " or "Name: None".
    assert project_element(make()) == "IFC class: IfcWall"


def test_no_trailing_newline() -> None:
    text = project_element(make(name="Wall"))
    assert not text.endswith("\n")
    assert text == "IFC class: IfcWall\nName: Wall"


@pytest.mark.parametrize("blank", [None, "", "   ", "\t\n "])
def test_blank_scalars_omit_their_line(blank: str | None) -> None:
    record = make(name="Kept", description=blank)
    assert "Description" not in project_element(record)
    assert "Name: Kept" in project_element(record)


def test_material_order_follows_ordinal_then_name() -> None:
    record = make(
        materials=[
            {"name": "zinco", "ordinal": 0},
            {"name": "alabastro", "ordinal": 1},
            {"name": "betume", "ordinal": 0},
        ]
    )
    # ElementRecord sorts by (ordinal or 0, name): betume/zinco at 0, alabastro at 1.
    assert "Materials: betume, zinco, alabastro" in project_element(record)


def test_empty_material_list_omits_the_line() -> None:
    assert "Materials" not in project_element(make(materials=[]))


def test_location_order_is_site_building_storey_space() -> None:
    record = make(
        location={
            "space": ref("S"),
            "storey": ref("T"),
            "building": ref("B"),
            "site": ref("A"),
        }
    )
    lines = project_element(record).splitlines()
    assert lines[1:] == ["Site: A", "Building: B", "Storey: T", "Space: S"]


def test_parent_element_is_never_projected() -> None:
    record = make(
        location={"storey": ref("Cripta"), "parent_element": {"name": "Muro Pai", "id": "el_x"}}
    )
    text = project_element(record)
    assert "Muro Pai" not in text
    assert "el_x" not in text


def test_identifier_provenance_and_metric_fields_are_excluded() -> None:
    """HBIM-005B §10.3 — a v1 boundary pinned by name, not a discovered gap."""
    record = make(
        name="Wall",
        metrics={"area": 12.5, "volume": 6.25, "height": 3.5, "thickness": 0.5},
        source={"source_id": "src-secret", "ifc_schema": "IFC4", "checksum": "sha256:beef"},
    )
    text = project_element(record)
    for excluded in (
        "el_0000000000000000000000000000test",
        "3HTEST0000000000000001",
        "proj-test",
        "1.0",
        "src-secret",
        "sha256:beef",
        "12.5",
        "3.5",
    ):
        assert excluded not in text, excluded
    assert set(PROJECTED_FIELDS) == {
        "ifc_class",
        "name",
        "description",
        "object_type",
        "predefined_type",
        "semantic_label",
        "materials.name",
        "location.site.name",
        "location.building.name",
        "location.storey.name",
        "location.space.name",
    }


def test_diacritics_and_case_survive_byte_for_byte() -> None:
    original = "Rosácea da fachada poente — ÍNGREME, ção, ãos"
    text = project_element(make(description=original))
    assert f"Description: {original}" in text
    # No NFC/NFKC folding, no accent stripping, no case folding.
    assert "Rosacea" not in text
    assert "rosácea" not in text


def test_projection_is_pure_and_repeatable() -> None:
    record = make(name="Coluna", description="Fuste liso", materials=[{"name": "mármore"}])
    assert project_element(record) == project_element(record)


def test_golden_texts_stay_under_the_authoring_cap() -> None:
    assert MAX_PROJECTED_CHARS == 2000
    long_record = make(description="x" * 1900)
    assert len(project_element(long_record)) <= MAX_PROJECTED_CHARS


def test_import_is_pure() -> None:
    """No socket, no ML package. Run in a subprocess so no module object is
    rebound for tests that already hold references to it."""
    import pathlib
    import subprocess
    import sys

    program = """
import socket, sys
def explode(*a, **k):
    raise AssertionError("import performed network activity")
socket.socket.connect = explode
socket.create_connection = explode
import eval.text_projection  # noqa: F401
banned = [m for m in ("torch", "sentence_transformers", "transformers",
                      "models.embeddings_qwen3", "opensearchpy") if m in sys.modules]
assert not banned, banned
print("PURE")
"""
    backend = pathlib.Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, "-c", program],
        capture_output=True,
        text=True,
        cwd=str(backend),
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    assert "PURE" in result.stdout
