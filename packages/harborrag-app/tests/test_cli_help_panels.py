"""The top-level help groups the commands by purpose, in a fixed panel order."""

from __future__ import annotations

import re

import pytest

from harborrag_app.cli import main as cli

pytestmark = pytest.mark.unit

_PANELS = {
    "Setup and diagnostics": ["me", "doctor"],
    "Access": ["auth"],
    "Chat": ["chat"],
    "Resources": [
        "projects",
        "source-types",
        "connections",
        "ingestions",
        "failures",
        "settings",
    ],
}
_PANEL_TITLE = re.compile(r"^╭─ (.+?) ─+╮$")
_COMMAND_ROW = re.compile(r"^│ ([a-z][a-z-]*)\s")


def _panels(help_text: str) -> dict[str, list[str]]:
    panels: dict[str, list[str]] = {}
    current: list[str] | None = None
    for line in help_text.splitlines():
        title = _PANEL_TITLE.match(line)
        if title:
            current = panels.setdefault(title.group(1), [])
        elif current is not None and (row := _COMMAND_ROW.match(line)):
            current.append(row.group(1))
    return panels


def test_each_command_is_listed_under_its_panel_in_order(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("COLUMNS", "200")

    assert cli.main(["--help"]) == 0

    panels = _panels(capsys.readouterr().out)
    command_panels = {name: commands for name, commands in panels.items() if name != "Options"}
    assert command_panels == _PANELS
    assert list(command_panels) == list(_PANELS)


def test_no_operations_panel_remains(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("COLUMNS", "200")

    assert cli.main(["--help"]) == 0

    assert "Operations" not in _panels(capsys.readouterr().out)
