"""HBIM-051 §11/§22 — pure projection ``r1``: goldens, v1 parity, truncation."""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest

from retrieval.rerank_projection import (
    MAX_RERANK_DOC_CHARS,
    RERANK_INSTRUCTION,
    RERANK_INSTRUCTION_VERSION,
    RERANK_PROJECTION_VERSION,
    SOURCE_FIELDS,
    project_source,
)

BACKEND = Path(__file__).resolve().parents[1]
MODULE = BACKEND / "retrieval" / "rerank_projection.py"


# --------------------------------------------------------------------------- #
# Hand-written goldens (§22 anti-tautology: expectations are literals)
# --------------------------------------------------------------------------- #
def test_golden_all_eleven_fields_present() -> None:
    source = {
        "ifc_class": "IfcWall",
        "name": "Parede Norte",
        "description": "Parede de alvenaria",
        "object_type": "Basic Wall",
        "predefined_type": "SOLIDWALL",
        "semantic_label": "parede exterior",
        "materials": [{"name": "calcário", "ordinal": 0}, {"name": "reboco", "ordinal": 1}],
        "location": {
            "site": {"name": "Convento"},
            "building": {"name": "Igreja"},
            "storey": {"name": "Piso 0"},
            "space": {"name": "Nave"},
        },
    }
    text, truncated = project_source(source)
    assert text == (
        "IFC class: IfcWall\n"
        "Name: Parede Norte\n"
        "Description: Parede de alvenaria\n"
        "Object type: Basic Wall\n"
        "Predefined type: SOLIDWALL\n"
        "Semantic label: parede exterior\n"
        "Materials: calcário, reboco\n"
        "Site: Convento\n"
        "Building: Igreja\n"
        "Storey: Piso 0\n"
        "Space: Nave"
    )
    assert truncated is False


def test_golden_every_optional_field_absent() -> None:
    text, truncated = project_source({"ifc_class": "IfcBeam"})
    assert text == "IFC class: IfcBeam"
    assert truncated is False


def test_golden_empty_materials_list_omits_the_line() -> None:
    text, _ = project_source({"ifc_class": "IfcSlab", "name": "Laje", "materials": []})
    assert text == "IFC class: IfcSlab\nName: Laje"


def test_golden_material_order_is_ordinal_then_name_with_none_as_zero() -> None:
    source = {
        "ifc_class": "IfcColumn",
        "materials": [
            {"name": "madeira de castanheiro", "ordinal": 1},
            {"name": "granito"},  # ordinal None -> 0: sorts before ordinal 1
            {"name": "azulejo", "ordinal": 0},  # ties with granito on 0 -> name order
        ],
    }
    text, _ = project_source(source)
    assert text == "IFC class: IfcColumn\nMaterials: azulejo, granito, madeira de castanheiro"


def test_golden_missing_space_omits_only_that_line() -> None:
    source = {
        "ifc_class": "IfcDoor",
        "location": {"site": {"name": "Solar"}, "storey": {"name": "1"}},
    }
    text, _ = project_source(source)
    assert text == "IFC class: IfcDoor\nSite: Solar\nStorey: 1"


def test_golden_accents_colons_and_newlines_are_verbatim() -> None:
    source = {
        "ifc_class": "IfcWindow",
        "name": "Janela: São João",
        "description": "linha um\nlinha dois",
    }
    text, _ = project_source(source)
    assert text == "IFC class: IfcWindow\nName: Janela: São João\nDescription: linha um\nlinha dois"


def test_empty_and_whitespace_values_are_omitted_like_v1() -> None:
    text, _ = project_source({"ifc_class": "IfcWall", "name": "   ", "description": ""})
    assert text == "IFC class: IfcWall"


def test_non_string_scalar_raises_without_echoing_the_value() -> None:
    with pytest.raises(ValueError) as excinfo:
        project_source({"ifc_class": 7})
    assert "ifc_class" in str(excinfo.value)
    assert "7" not in str(excinfo.value)


def test_malformed_materials_raise() -> None:
    with pytest.raises(ValueError):
        project_source({"ifc_class": "IfcWall", "materials": [{"name": 3}]})
    with pytest.raises(ValueError):
        project_source({"ifc_class": "IfcWall", "materials": [{"name": "x", "ordinal": True}]})
    with pytest.raises(ValueError):
        project_source({"ifc_class": "IfcWall", "materials": {"name": "x"}})


# --------------------------------------------------------------------------- #
# §C6 — byte-equality with the frozen v1 projection on all 122 gold elements
# --------------------------------------------------------------------------- #
def test_rerank_projection_equals_frozen_projection_v1_on_all_122() -> None:
    from eval.run_semantic_baseline import verify_preregistration
    from eval.text_projection import project_element

    gold = verify_preregistration()
    assert len(gold.corpus) == 122
    for record in gold.corpus:
        source = record.model_dump(mode="json", exclude_none=True)
        r1_text, truncated = project_source(source)
        assert r1_text == project_element(record), record.element_id
        assert truncated is False, record.element_id


# --------------------------------------------------------------------------- #
# Truncation (§11.6)
# --------------------------------------------------------------------------- #
def test_truncation_is_exact_tail_cut_keeping_the_identifying_head() -> None:
    source = {"ifc_class": "IfcWall", "name": "W1", "description": "x" * 3000}
    text, truncated = project_source(source)
    assert truncated is True
    assert len(text) == MAX_RERANK_DOC_CHARS
    assert text.startswith("IFC class: IfcWall\nName: W1\nDescription: ")
    # Idempotent: projecting the same source twice gives the same bytes.
    again, again_truncated = project_source(source)
    assert (again, again_truncated) == (text, truncated)


def test_text_exactly_at_the_bound_is_not_truncated() -> None:
    prefix = "IFC class: IfcWall\nDescription: "
    source = {"ifc_class": "IfcWall", "description": "y" * (MAX_RERANK_DOC_CHARS - len(prefix))}
    text, truncated = project_source(source)
    assert len(text) == MAX_RERANK_DOC_CHARS
    assert truncated is False


# --------------------------------------------------------------------------- #
# Purity, determinism, constants
# --------------------------------------------------------------------------- #
def test_pinned_constants() -> None:
    assert RERANK_PROJECTION_VERSION == "r1"
    assert RERANK_INSTRUCTION_VERSION == "v1"
    assert RERANK_INSTRUCTION == (
        "Given a query about a historic building information model, "
        "retrieve the building elements that satisfy it"
    )
    assert MAX_RERANK_DOC_CHARS == 2000
    assert SOURCE_FIELDS == (
        "ifc_class",
        "name",
        "description",
        "object_type",
        "predefined_type",
        "semantic_label",
        "materials",
        "location.site.name",
        "location.building.name",
        "location.storey.name",
        "location.space.name",
    )


def test_instruction_matches_the_settings_default() -> None:
    from shared.config import RerankerSettings

    assert RerankerSettings(_env_file=None).instruction == RERANK_INSTRUCTION


def test_module_is_pure_stdlib_by_ast() -> None:
    tree = ast.parse(MODULE.read_text(encoding="utf-8"))
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imports.add((node.module or "").split(".")[0])
    assert imports <= {"__future__", "typing"}, imports
    forbidden_calls = {"open", "eval", "exec", "__import__"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            assert node.func.id not in forbidden_calls, node.func.id


def test_production_modules_do_not_import_eval() -> None:
    for path in (
        MODULE,
        BACKEND / "retrieval" / "rerank.py",
        BACKEND / "models" / "reranker_qwen3.py",
        BACKEND / "api" / "main.py",
        BACKEND / "api" / "search.py",
    ):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                assert not (node.module or "").startswith("eval"), (path.name, node.module)
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert not alias.name.startswith("eval"), (path.name, alias.name)


def test_fresh_subprocess_import_with_socket_bomb() -> None:
    code = (
        "import socket\n"
        "def bomb(*a, **k): raise AssertionError('socket during import')\n"
        "socket.socket = bomb\n"
        "import retrieval.rerank_projection as m\n"
        "assert m.project_source({'ifc_class': 'IfcWall'})[0] == 'IFC class: IfcWall'\n"
        "print('OK')\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=str(BACKEND.parent),
        env={"PYTHONPATH": str(BACKEND), "PATH": "/usr/bin:/bin"},
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "OK"


@pytest.mark.parametrize("seed", ["0", "1", "7", "4242"])
def test_deterministic_under_pythonhashseed(seed: str) -> None:
    code = (
        "from retrieval.rerank_projection import project_source\n"
        "src = {'ifc_class': 'IfcWall', 'materials': [{'name': 'b'}, {'name': 'a'}],\n"
        "       'location': {'site': {'name': 'S'}}}\n"
        "print(repr(project_source(src)))\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=str(BACKEND.parent),
        env={"PYTHONPATH": str(BACKEND), "PYTHONHASHSEED": seed, "PATH": "/usr/bin:/bin"},
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == (
        "('IFC class: IfcWall\\nMaterials: a, b\\nSite: S', False)"
    )
