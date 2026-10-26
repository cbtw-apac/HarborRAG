"""Path arguments resolve from where the user ran the command, not the activated root.

`main()` moves into the project before Click parses argv, so without rebasing a relative
`cases.jsonl` typed in a project subdirectory would be looked up at the project root.
Discovery also stops at the nearest marker, so a checkout is not outranked by a stray
`harborrag.yaml` in an ancestor.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from project_test_support import scaffold

from harborrag_app.cli import main as cli
from harborrag_app.cli import project as project_module
from harborrag_app.cli.environment import checkout_write_refusal
from harborrag_engine.security import CreatedKey
from harborrag_runtime.security import api_key_operations


@pytest.fixture
def in_project_subdirectory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = scaffold(tmp_path / "proj")
    sub = root / "sub"
    sub.mkdir()
    monkeypatch.chdir(sub)
    monkeypatch.delenv("HARBORRAG_PROJECT", raising=False)
    return sub.resolve()


def test_a_relative_cases_file_is_read_from_the_invocation_directory(
    in_project_subdirectory: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    from harborrag_runtime.topology import operations

    (in_project_subdirectory / "cases.jsonl").write_text('{"case": 1}\n', encoding="utf-8")

    async def fake_evaluate(payload: str) -> dict[str, str]:
        return {"payload": payload}

    monkeypatch.setattr(operations, "evaluate", fake_evaluate)

    assert cli.main(["topology", "evaluate", "cases.jsonl"]) == 0

    captured = capsys.readouterr()
    assert "using project" in captured.err  # activation did move the process
    assert json.loads(captured.out)["data"] == {"payload": '{"case": 1}\n'}


def test_a_relative_permissions_file_is_checked_against_the_invocation_directory(
    in_project_subdirectory: Path, capsys
) -> None:
    """`exists=True` runs inside the type, so the rebase must happen before it."""

    (in_project_subdirectory.parent / "only-at-root.json").write_text("{}", encoding="utf-8")

    code = cli.main(["topology", "indexing", "permissions-import", "only-at-root.json"])

    assert code == 2
    assert "does not exist" in capsys.readouterr().err


def test_a_relative_secret_output_lands_where_the_user_stood_and_is_reported_absolute(
    in_project_subdirectory: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    from harborrag_app.cli.commands import auth_keys

    created = CreatedKey(
        key_id="0123456789abcdef01234567",
        tenant_id="t",
        owner="user-x",
        name="k",
        expires_at=datetime(2026, 12, 1, tzinfo=UTC),
        raw_key="hrk_dev_v1_0123456789abcdef01234567." + "a" * 43,
    )
    monkeypatch.setattr(api_key_operations, "ensure_key_schema", AsyncMock(return_value=False))
    monkeypatch.setattr(api_key_operations, "create_key", AsyncMock(return_value=created))
    monkeypatch.setattr(auth_keys, "operator_identity", lambda: "op@host")

    code = cli.main(
        ["auth", "keys", "create", "--tenant", "t", "--owner", "user-x", "--name", "k",
         "--expires-in", "1d", "--secret-output", "./k.key"]
    )  # fmt: skip

    assert code == 0, capsys.readouterr().err
    expected = in_project_subdirectory / "k.key"
    assert expected.is_file()
    assert not (in_project_subdirectory.parent / "k.key").exists()
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["data"]["secret_written_to"] == str(expected)


def test_invocation_cwd_falls_back_to_the_cwd_once_the_process_moves_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(project_module, "_INVOCATION", (tmp_path / "origin", tmp_path))
    monkeypatch.chdir(tmp_path)
    assert project_module.invocation_cwd() == tmp_path / "origin"

    (tmp_path / "elsewhere").mkdir()
    monkeypatch.chdir(tmp_path / "elsewhere")
    assert project_module.invocation_cwd() == (tmp_path / "elsewhere").resolve()


def _checkout_under_a_stray_project(tmp_path: Path) -> tuple[Path, Path]:
    home = scaffold(tmp_path / "h")
    checkout = home / "co"
    (checkout / "config").mkdir(parents=True)
    (checkout / "config" / "connectors.yaml").write_text("connectors: {}\n", encoding="utf-8")
    return home.resolve(), checkout.resolve()


def test_a_nearer_checkout_stops_the_walk_before_an_ancestor_marker(tmp_path: Path) -> None:
    _home, checkout = _checkout_under_a_stray_project(tmp_path)
    (checkout / "src").mkdir()

    assert project_module.find_project(checkout) is None
    assert project_module.find_project(checkout / "src") is None
    assert project_module.find_legacy_checkout(checkout / "src") == checkout


def test_a_projects_own_config_directory_still_resolves_to_the_project(tmp_path: Path) -> None:
    """Every scaffolded project ships config/connectors.yaml next to its marker."""

    home, _checkout = _checkout_under_a_stray_project(tmp_path)
    (home / "config").mkdir()
    (home / "config" / "connectors.yaml").write_text("connectors: {}\n", encoding="utf-8")

    project = project_module.find_project(home / "config")

    assert project is not None and project.root == home


def test_main_keeps_the_checkout_and_its_write_guard_under_a_stray_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    _home, checkout = _checkout_under_a_stray_project(tmp_path)
    monkeypatch.chdir(checkout)
    monkeypatch.delenv("HARBORRAG_PROJECT", raising=False)
    monkeypatch.delenv("HARBORRAG_CONTROL_DB_URL", raising=False)

    cli.main(["topology", "evaluate", "nope"])

    assert "using project" not in capsys.readouterr().err
    assert project_module.active_project() is None
    assert checkout_write_refusal() is not None
