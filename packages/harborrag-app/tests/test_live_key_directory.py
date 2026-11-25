"""The key output directory HarborRAG creates under home is owner-only at every level."""

from __future__ import annotations

import os
import stat
from collections.abc import Iterator
from pathlib import Path

import pytest

from harborrag_app.cli.commands.auth_keys import check_secret_output, default_secret_output

pytestmark = [
    pytest.mark.unit,
    pytest.mark.skipif(os.name != "posix", reason="POSIX directory modes"),
]


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    previous = os.umask(0o022)
    try:
        yield home
    finally:
        os.umask(previous)


def test_check_secret_output_on_a_fresh_home_creates_every_directory_owner_only(
    home: Path,
) -> None:
    check_secret_output(default_secret_output("demo-reader"))

    assert _mode(home / ".harborrag") == 0o700
    assert _mode(home / ".harborrag" / "keys") == 0o700


def test_check_secret_output_keeps_the_mode_of_an_existing_directory(home: Path) -> None:
    (home / ".harborrag").mkdir()
    os.chmod(home / ".harborrag", 0o750)

    check_secret_output(default_secret_output("demo-reader"))

    assert _mode(home / ".harborrag") == 0o750
    assert _mode(home / ".harborrag" / "keys") == 0o700
