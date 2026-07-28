"""Tests for the physical Python file-length gate."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
import scripts.check_python_file_length as file_length


def test_physical_line_count_includes_blank_and_unterminated_lines(
    tmp_path: Path,
) -> None:
    target = tmp_path / "sample.py"
    target.write_bytes(b"import os\n\nvalue = 1")

    assert file_length.physical_line_count(target) == 3


def test_oversized_files_reports_only_values_above_maximum(tmp_path: Path) -> None:
    allowed = tmp_path / "allowed.py"
    oversized = tmp_path / "oversized.py"
    allowed.write_text("x\n" * 3, encoding="utf-8")
    oversized.write_text("x\n" * 4, encoding="utf-8")

    assert file_length.oversized_files(
        (oversized, allowed),
        maximum=3,
        root=tmp_path,
    ) == ("oversized.py: 4 lines (maximum 3)",)


def test_python_files_includes_tracked_and_untracked_results(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "tracked.py").touch()
    (tmp_path / "new.py").touch()
    monkeypatch.setattr(
        file_length.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(stdout=b"tracked.py\0new.py\0ignored.py\0"),
    )

    assert file_length.python_files(tmp_path) == (
        tmp_path / "tracked.py",
        tmp_path / "new.py",
    )
