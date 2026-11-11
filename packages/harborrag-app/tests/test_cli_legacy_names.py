"""Old ``source`` and ``ingest`` names fail with a hint; ``retrieve`` and ``topology`` are gone."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from harborrag_app.cli import main as cli
from harborrag_app.cli.commands.legacy import LEGACY

pytestmark = pytest.mark.unit

_COMMANDS_IN_HELP = (
    "me",
    "projects",
    "source-types",
    "connections",
    "ingestions",
    "failures",
    "settings",
    "chat",
    "config",
    "doctor",
    "auth",
)


@pytest.fixture(autouse=True)
def _outside_a_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("HARBORRAG_PROJECT", raising=False)


@pytest.mark.parametrize(("pair", "replacement"), sorted(LEGACY.items()))
def test_old_name_exits_2_and_names_the_new_command(
    pair: tuple[str, str], replacement: str, capsys: pytest.CaptureFixture[str]
) -> None:
    group, action = pair

    code = cli.main([group, action, "demo/jira-main", "--json", "--wait", "-x"])

    captured = capsys.readouterr()
    assert code == 2
    assert captured.out == ""
    assert captured.err.strip() == (
        f"error: 'harborrag {group} {action}' was renamed; use 'harborrag {replacement}'"
    )


@pytest.mark.parametrize("argv", [["retrieve", "x"], ["topology", "status"]])
def test_removed_commands_are_unknown(argv: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(argv) == 2

    assert "No such command" in capsys.readouterr().err


def test_help_lists_only_the_spec_commands(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["--help"]) == 0

    out = capsys.readouterr().out
    listed = set(re.findall(r"^│ ([a-z][a-z-]*)\s", out, flags=re.MULTILINE))
    assert listed == set(_COMMANDS_IN_HELP)


def test_no_output_string_names_an_old_command() -> None:
    source = Path(__file__).parents[1] / "src"
    legacy_table = source / "harborrag_app" / "cli" / "commands" / "legacy.py"
    pattern = re.compile(
        r"harborrag (source|ingest) "
        r"|\bingest (run|start|status|wait|watch|pause|resume|cancel)\b"
        r"|\bsource (list|show|add|update|test|sync|remove)\b"
    )

    hits = [
        f"{path}:{number}"
        for path in sorted(source.rglob("*.py"))
        if path != legacy_table
        for number, line in enumerate(path.read_text("utf-8").splitlines(), start=1)
        if pattern.search(line)
    ]

    assert hits == []
