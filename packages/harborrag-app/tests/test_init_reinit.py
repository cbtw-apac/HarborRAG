"""Re-running `harborrag init` keeps what the user already has.

An existing .gitignore is extended, never replaced (every git repository has one), an
existing docker-compose.yml is kept like .env, and a re-run keeps the port offset the
project already uses instead of treating its own running stack as someone else's.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from harborrag_app.cli import main as cli
from harborrag_app.cli.commands import init_support


def _qdrant_port(text: str) -> list[str]:
    return re.findall(r"HARBORRAG_QDRANT_URL=http://localhost:(\d+)", text) or re.findall(
        r"127\.0\.0\.1:(\d+):6333", text
    )


@pytest.fixture
def free_ports(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(init_support, "_busy_ports", lambda ports: [])


def test_init_in_a_repository_with_a_gitignore_needs_no_force_and_keeps_its_rules(
    tmp_path: Path, free_ports: None
) -> None:
    (tmp_path / ".gitignore").write_text("node_modules/\n*.pyc\nsecrets.json", encoding="utf-8")

    assert cli.main(["init", str(tmp_path), "--yes"]) == 0

    lines = (tmp_path / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert lines[:3] == ["node_modules/", "*.pyc", "secrets.json"]
    assert {".env", ".env.new", ".harborrag/"} <= set(lines)


def test_force_extends_the_gitignore_once_and_never_duplicates_lines(
    tmp_path: Path, free_ports: None
) -> None:
    (tmp_path / ".gitignore").write_text("secrets.json\n.env\n", encoding="utf-8")

    assert cli.main(["init", str(tmp_path), "--yes"]) == 0
    assert cli.main(["init", str(tmp_path), "--yes", "--force"]) == 0

    lines = (tmp_path / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert lines == ["secrets.json", ".env", ".env.new", ".harborrag/"]


def test_force_keeps_an_existing_compose_file_and_writes_the_new_one_beside_it(
    tmp_path: Path, free_ports: None, capsys
) -> None:
    (tmp_path / "docker-compose.yml").write_text("services: {mine: {}}\n", encoding="utf-8")

    assert cli.main(["init", str(tmp_path), "--yes"]) == 1  # still a reason to ask first
    refusal = " ".join(capsys.readouterr().err.split())
    assert "--force" in refusal and "to overwrite" not in refusal
    assert "docker-compose.yml are kept" in refusal and "docker-compose.harborrag.yml" in refusal
    assert cli.main(["init", str(tmp_path), "--yes", "--force"]) == 0

    assert (tmp_path / "docker-compose.yml").read_text() == "services: {mine: {}}\n"
    assert "qdrant" in (tmp_path / "docker-compose.harborrag.yml").read_text()
    assert "docker-compose.harborrag.yml" in capsys.readouterr().out


def test_reinit_keeps_the_projects_port_offset_while_its_own_stack_holds_the_ports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    monkeypatch.setattr(
        init_support, "_busy_ports", lambda ports: [p for p in ports if p[0] < 10000]
    )
    assert cli.main(["init", str(tmp_path), "--yes"]) == 0
    assert _qdrant_port((tmp_path / ".env").read_text()) == ["16333"]
    # The project's stack is now up at offset 10000, so every probed port looks taken.
    monkeypatch.setattr(init_support, "_busy_ports", lambda ports: list(ports))

    assert cli.main(["init", str(tmp_path), "--yes", "--force"]) == 0

    assert _qdrant_port((tmp_path / ".env").read_text()) == ["16333"]
    assert _qdrant_port((tmp_path / ".env.new").read_text()) == ["16333"]
    assert _qdrant_port((tmp_path / "docker-compose.harborrag.yml").read_text()) == ["16333"]
    assert "Keeping port offset 10000" in capsys.readouterr().out


def test_an_explicit_ports_offset_still_wins_on_reinit_and_says_env_disagrees(
    tmp_path: Path, free_ports: None, capsys
) -> None:
    assert cli.main(["init", str(tmp_path), "--yes", "--ports-offset", "10000"]) == 0
    capsys.readouterr()

    code = cli.main(["init", str(tmp_path), "--yes", "--force", "--ports-offset", "20000"])

    assert code == 0
    assert _qdrant_port((tmp_path / ".env.new").read_text()) == ["26333"]
    out = " ".join(capsys.readouterr().out.split())
    assert "uses port offset 10000 and is kept" in out


def test_the_recorded_offset_falls_back_to_the_compose_file_and_ignores_nonsense(
    tmp_path: Path,
) -> None:
    assert init_support.recorded_ports_offset(tmp_path) is None
    (tmp_path / "docker-compose.yml").write_text('    - "127.0.0.1:26333:6333"\n')
    assert init_support.recorded_ports_offset(tmp_path) == 20000
    (tmp_path / ".env").write_text("HARBORRAG_QDRANT_URL=http://localhost:6333\n")
    assert init_support.recorded_ports_offset(tmp_path) == 0
    (tmp_path / ".env").write_text("HARBORRAG_QDRANT_URL=http://localhost:80\n")
    assert init_support.recorded_ports_offset(tmp_path) is None
