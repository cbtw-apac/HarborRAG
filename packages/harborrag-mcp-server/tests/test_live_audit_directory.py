"""The durable audit directory is checked when the MCP server starts, not on the first call."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

import harborrag_mcp_server.__main__ as cli

pytestmark = [
    pytest.mark.unit,
    pytest.mark.skipif(os.name != "posix", reason="POSIX directory modes"),
]


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_start_with_a_group_readable_audit_directory_refuses_and_keeps_its_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    directory = tmp_path / "audit"
    directory.mkdir()
    os.chmod(directory, 0o755)
    monkeypatch.setenv("HARBORRAG_MCP_AUDIT_PATH", str(directory / "mcp-audit.jsonl"))

    with pytest.raises(SystemExit) as raised:
        cli.main(["--check"])

    assert raised.value.code == 2
    error = capsys.readouterr().err
    assert str(directory) in error
    assert "0700" in error
    assert _mode(directory) == 0o755


def test_start_with_a_missing_audit_directory_creates_it_owner_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / "missing" / "audit"
    monkeypatch.setenv("HARBORRAG_MCP_AUDIT_PATH", str(directory / "mcp-audit.jsonl"))

    assert cli.main(["--check"]) == 0

    assert _mode(directory) == 0o700


def test_start_with_a_relative_audit_path_checks_it_under_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    launch = tmp_path / "launch"
    launch.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(launch)
    monkeypatch.setenv("HARBORRAG_MCP_AUDIT_PATH", ".harborrag/mcp-audit.jsonl")

    assert cli.main(["--check"]) == 0

    assert _mode(home / ".harborrag") == 0o700
    assert not (launch / ".harborrag").exists()
