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

pytestmark = [pytest.mark.slow, pytest.mark.contract, pytest.mark.timeout(30)]

_CORE_SRC = Path(__file__).resolve().parents[1] / "src"

_PRELUDE = """\
from harborrag_core.ingestion import (
    ArtifactReference,
    DocumentArtifactSlot,
    DocumentVersionState,
)
from harborrag_core.ports.document_release import DocumentVersionPort
"""

# Each case is one ``port.transition`` call; mypy errors are attributed to a case
# by the line the call starts on.
_CASES = {
    "slot_pair": "artifact=(DocumentArtifactSlot.CANONICAL, ref)",
    "column_string": 'artifact=("canonical_artifact", ref)',
    "column_keyword": 'artifact_column="canonical_artifact", artifact=ref',
}


def _snippet() -> tuple[str, dict[str, int]]:
    lines = _PRELUDE.splitlines()
    call_lines: dict[str, int] = {}
    for name, arguments in _CASES.items():
        lines += [
            "",
            "",
            f"async def {name}(port: DocumentVersionPort, ref: ArtifactReference) -> None:",
        ]
        call_lines[name] = len(lines) + 1
        lines.append(
            f'    await port.transition("v-1", DocumentVersionState.CANONICAL_READY, {arguments})'
        )
    return "\n".join(lines) + "\n", call_lines


@pytest.fixture(scope="module")
def errors_by_case(tmp_path_factory: pytest.TempPathFactory) -> dict[str, list[str]]:
    api = pytest.importorskip("mypy.api")
    workdir = tmp_path_factory.mktemp("slot_contract")
    source, call_lines = _snippet()
    snippet = workdir / "slot_usage.py"
    snippet.write_text(source, encoding="utf-8")
    config = workdir / "mypy.ini"
    config.write_text(f"[mypy]\nmypy_path = {_CORE_SRC}\n", encoding="utf-8")
    stdout, _stderr, _status = api.run(
        [
            "--no-incremental",
            "--cache-dir",
            str(workdir / ".mypy_cache"),
            "--config-file",
            str(config),
            "--follow-imports",
            "silent",
            "--python-version",
            "3.12",
            str(snippet),
        ],
    )
    errors: dict[str, list[str]] = {name: [] for name in _CASES}
    for line in stdout.splitlines():
        parts = line.split(":", 3)
        if len(parts) < 4 or not parts[0].endswith("slot_usage.py") or "error" not in parts[2]:
            continue
        for name, call_line in call_lines.items():
            if int(parts[1]) == call_line:
                errors[name].append(line)
    unattributed = [
        line
        for line in stdout.splitlines()
        if "slot_usage.py" in line
        and ": error:" in line
        and not any(line in found for found in errors.values())
    ]
    assert unattributed == [], unattributed
    return errors


def test_port_accepts_a_slot_and_reference_pair(errors_by_case: dict[str, list[str]]) -> None:
    assert errors_by_case["slot_pair"] == []


def test_port_rejects_a_column_name_string_where_a_slot_is_expected(
    errors_by_case: dict[str, list[str]],
) -> None:
    errors = errors_by_case["column_string"]
    assert any("arg-type" in line for line in errors), errors


def test_port_no_longer_accepts_the_artifact_column_keyword(
    errors_by_case: dict[str, list[str]],
) -> None:
    errors = errors_by_case["column_keyword"]
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
