"""Tests for scripts/check_ruff_complexity.py."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import scripts.check_ruff_complexity as complexity_check
from scripts.check_ruff_complexity import (
    aggregate_diagnostics,
    compare_counts,
    load_baseline,
)

pytestmark = pytest.mark.unit


def diagnostic(path: Path, code: str) -> dict[str, object]:
    return {
        "filename": str(path),
        "code": code,
    }


def test_aggregates_diagnostics_by_relative_file_and_rule(tmp_path: Path) -> None:
    target = tmp_path / "package" / "client.py"
    diagnostics = [
        diagnostic(target, "PLR0913"),
        diagnostic(target, "PLR0913"),
        diagnostic(target, "C901"),
    ]

    assert aggregate_diagnostics(diagnostics, root=tmp_path) == {
        "package/client.py": {
            "C901": 1,
            "PLR0913": 2,
        }
    }


def test_rejects_unexpected_ruff_rule(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="unsupported Ruff complexity rule"):
        aggregate_diagnostics(
            [diagnostic(tmp_path / "module.py", "F401")],
            root=tmp_path,
        )


def test_exact_baseline_match_passes() -> None:
    counts = {"package/client.py": {"C901": 1, "PLR0913": 2}}

    assert compare_counts(counts, counts) == []


def test_new_file_violation_is_a_regression() -> None:
    failures = compare_counts(
        {"package/new_client.py": {"C901": 1}},
        {},
    )

    assert failures == [
        "package/new_client.py: C901 increased from 0 to 1",
    ]


def test_increased_count_is_a_regression() -> None:
    failures = compare_counts(
        {"package/client.py": {"PLR0913": 3}},
        {"package/client.py": {"PLR0913": 2}},
    )

    assert failures == [
        "package/client.py: PLR0913 increased from 2 to 3",
    ]


def test_reduction_requires_downward_baseline_update() -> None:
    failures = compare_counts(
        {"package/client.py": {"C901": 1}},
        {"package/client.py": {"C901": 2}},
    )

    assert failures == [
        "package/client.py: C901 decreased from 2 to 1; update the baseline downward",
    ]


def test_removed_baseline_entry_passes_after_update() -> None:
    assert compare_counts({}, {}) == []


def test_repository_files_include_untracked_and_ignore_deletions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "kept.py").touch()

    monkeypatch.setattr(
        complexity_check.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            stdout=b"kept.py\0deleted.py\0",
        ),
    )

    assert complexity_check.repository_python_files(tmp_path) == ("kept.py",)


def test_rejects_unsupported_baseline_version(tmp_path: Path) -> None:
    baseline = tmp_path / "baseline.json"
    baseline.write_text(
        json.dumps({"version": 2, "files": {}}),
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="unsupported complexity baseline"):
        load_baseline(baseline)


def test_rejects_baseline_without_file_mapping(tmp_path: Path) -> None:
    baseline = tmp_path / "baseline.json"
    baseline.write_text(
        json.dumps({"version": 1}),
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="files mapping"):
        load_baseline(baseline)


def test_quality_workflow_runs_explicit_architecture_gates() -> None:
    repository = Path(__file__).resolve().parents[1]
    workflow = (repository / ".github/workflows/quality-gates.yml").read_text(encoding="utf-8")
    makefile = (repository / "Makefile").read_text(encoding="utf-8")

    assert "uv run make import-boundaries" in workflow
    assert "uv run make complexity" in workflow
    assert "uv run make file-length" in workflow
    assert "ruff check --ignore C901,PLR0913 ." in makefile
