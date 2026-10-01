"""Type-level contract: document-version artifacts are addressed by slot, not column.

Package tests are excluded from the repository mypy run, so this module checks a
usage snippet with mypy directly. A bare column-name string where a
``DocumentArtifactSlot`` is expected must be a type error, and the old
``artifact_column`` keyword must no longer exist on the port.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from harborrag_core.ingestion import DocumentArtifactSlot

_CORE_SRC = Path(__file__).resolve().parents[1] / "src"

_PRELUDE = """
from harborrag_core.ingestion import (
    ArtifactReference,
    DocumentArtifactSlot,
    DocumentVersionState,
)
from harborrag_core.ports.document_release import DocumentVersionPort


async def use(port: DocumentVersionPort, ref: ArtifactReference) -> None:
"""


def _mypy_errors(body: str, tmp_path: Path) -> list[str]:
    api = pytest.importorskip("mypy.api")
    snippet = tmp_path / "slot_usage.py"
    snippet.write_text(_PRELUDE + body, encoding="utf-8")
    stdout, _stderr, _status = api.run(
        [
            "--no-incremental",
            "--cache-dir",
            str(tmp_path / ".mypy_cache"),
            "--config-file",
            "",
            "--follow-imports",
            "silent",
            "--python-version",
            "3.12",
            str(snippet),
        ],
    )
    return [line for line in stdout.splitlines() if "slot_usage.py" in line and "error" in line]


@pytest.fixture
def core_mypy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MYPYPATH", str(_CORE_SRC))


@pytest.mark.usefixtures("core_mypy_path")
def test_port_accepts_a_slot_and_reference_pair(tmp_path: Path) -> None:
    errors = _mypy_errors(
        "    await port.transition(\n"
        '        "version-1",\n'
        "        DocumentVersionState.CANONICAL_READY,\n"
        "        artifact=(DocumentArtifactSlot.CANONICAL, ref),\n"
        "    )\n",
        tmp_path,
    )
    assert errors == []


@pytest.mark.usefixtures("core_mypy_path")
def test_port_rejects_a_column_name_string_where_a_slot_is_expected(
    tmp_path: Path,
) -> None:
    errors = _mypy_errors(
        "    await port.transition(\n"
        '        "version-1",\n'
        "        DocumentVersionState.CANONICAL_READY,\n"
        '        artifact=("canonical_artifact", ref),\n'
        "    )\n",
        tmp_path,
    )
    assert any("arg-type" in line for line in errors), errors


@pytest.mark.usefixtures("core_mypy_path")
def test_port_no_longer_accepts_the_artifact_column_keyword(tmp_path: Path) -> None:
    errors = _mypy_errors(
        "    await port.transition(\n"
        '        "version-1",\n'
        "        DocumentVersionState.CANONICAL_READY,\n"
        '        artifact_column="canonical_artifact",\n'
        "        artifact=ref,\n"
        "    )\n",
        tmp_path,
    )
    assert any("artifact_column" in line for line in errors), errors


def test_slot_set_is_closed() -> None:
    assert {slot.value for slot in DocumentArtifactSlot} == {
        "RAW",
        "RAW_METADATA",
        "CANONICAL",
        "CHUNK",
        "CHUNK_INDEX",
        "RELATION",
        "REPRESENTATION",
    }
