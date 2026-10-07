"""MCP reader keys are issued to a private file and administered without exposing secrets."""

from __future__ import annotations

import json
import os
import re
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from typer.testing import CliRunner

from harborrag_app.cli.commands import auth_keys
from harborrag_app.cli.main import app
from harborrag_core.security.api_keys import parse_key
from harborrag_engine.security import CreatedKey
from harborrag_runtime.security import api_key_operations

EXPIRES = datetime(2026, 12, 1, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _schema_present(monkeypatch):
    """Every command checks the key table first; assume it exists unless a test says otherwise."""

    monkeypatch.setattr(api_key_operations, "ensure_key_schema", AsyncMock(return_value=False))


def _created() -> CreatedKey:
    return CreatedKey(
        key_id="0123456789abcdef01234567",
        tenant_id="engineering",
        owner="user-huy",
        name="huy-laptop",
        expires_at=EXPIRES,
        raw_key="hrk_dev_v1_0123456789abcdef01234567." + "a" * 43,
    )


def test_help_lists_the_three_commands():
    result = CliRunner().invoke(app, ["auth", "keys", "--help"])

    assert result.exit_code == 0
    for name in ("create", "list", "revoke"):
        assert name in result.stdout


@pytest.mark.parametrize(
    ("text", "expected"),
    [("90d", timedelta(days=90)), ("12h", timedelta(hours=12)), ("30m", timedelta(minutes=30))],
)
def test_duration_forms(text, expected):
    assert auth_keys.parse_duration(text) == expected


@pytest.mark.parametrize("text", ["", "90", "1w", "d", "-1d"])
def test_bad_durations_are_rejected(text):
    with pytest.raises(Exception, match="d, h or m"):
        auth_keys.parse_duration(text)


def test_create_writes_the_secret_to_a_private_file_and_prints_only_safe_fields(
    tmp_path: Path, monkeypatch
):
    create = AsyncMock(return_value=_created())
    monkeypatch.setattr(api_key_operations, "create_key", create)
    monkeypatch.setattr(auth_keys, "operator_identity", lambda: "op@host")
    output = tmp_path / "huy-laptop.key"

    result = CliRunner().invoke(
        app,
        [
            "auth", "keys", "create",
            "--tenant", "engineering",
            "--owner", "user-huy",
            "--name", "huy-laptop",
            "--expires-in", "30d",
            "--secret-output", str(output),
        ],
    )  # fmt: skip

    assert result.exit_code == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["ok"] and payload["data"]["key_id"] == "0123456789abcdef01234567"
    assert "hrk_" not in result.stdout
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    written = output.read_text(encoding="ascii").strip()
    assert parse_key(written) is not None and written == _created().raw_key
    assert create.call_args.kwargs == {
        "operator": "op@host",
        "tenant_id": "engineering",
        "owner": "user-huy",
        "name": "huy-laptop",
        "lifetime": timedelta(days=30),
    }


def test_create_refuses_an_existing_output_file_before_touching_the_store(
    tmp_path: Path, monkeypatch
):
    create = AsyncMock(return_value=_created())
    monkeypatch.setattr(api_key_operations, "create_key", create)
    output = tmp_path / "exists.key"
    output.write_text("old\n", encoding="utf-8")

    result = CliRunner().invoke(
        app,
        ["auth", "keys", "create", "--tenant", "t", "--owner", "user-x", "--name", "k",
         "--expires-in", "1d", "--secret-output", str(output)],
    )  # fmt: skip

    assert result.exit_code != 0
    assert "already exists" in result.output
    create.assert_not_called()
    assert output.read_text(encoding="utf-8") == "old\n"


def test_a_failed_secret_write_revokes_the_key_it_just_created(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(api_key_operations, "create_key", AsyncMock(return_value=_created()))
    revoke = AsyncMock(return_value=True)
    monkeypatch.setattr(api_key_operations, "revoke_key", revoke)
    monkeypatch.setattr(auth_keys, "operator_identity", lambda: "op@host")

    def explode(path: Path, secret: str) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(auth_keys, "write_secret_file", explode)

    result = CliRunner().invoke(
        app,
        ["auth", "keys", "create", "--tenant", "t", "--owner", "user-x", "--name", "k",
         "--expires-in", "1d", "--secret-output", str(tmp_path / "new.key")],
    )  # fmt: skip

    assert result.exit_code == 1
    assert revoke.call_args.kwargs["key_id"] == "0123456789abcdef01234567"
    assert revoke.call_args.kwargs["reason"] == "secret delivery failed"
    assert "hrk_" not in result.output


def test_list_delegates_and_shows_no_secret_material(monkeypatch):
    rows = [
        {"key_id": "0" * 24, "name": "laptop", "owner": "user-huy", "state": "active"},
        {"key_id": "1" * 24, "name": "old", "owner": "user-huy", "state": "revoked"},
    ]
    monkeypatch.setattr(api_key_operations, "list_keys", AsyncMock(return_value=rows))

    result = CliRunner().invoke(app, ["auth", "keys", "list", "--tenant", "engineering"])

    assert result.exit_code == 0
    assert json.loads(result.stdout)["data"]["keys"] == rows
    assert "secret_hash" not in result.stdout


def test_revoke_by_id_is_idempotent_and_by_owner_reports_ids(monkeypatch):
    monkeypatch.setattr(auth_keys, "operator_identity", lambda: "op@host")
    revoke = AsyncMock(side_effect=[True, False])
    monkeypatch.setattr(api_key_operations, "revoke_key", revoke)

    first = CliRunner().invoke(
        app, ["auth", "keys", "revoke", "--key-id", "0" * 24, "--reason", "lost"]
    )
    second = CliRunner().invoke(
        app, ["auth", "keys", "revoke", "--key-id", "0" * 24, "--reason", "lost"]
    )

    assert first.exit_code == 0 and json.loads(first.stdout)["data"]["revoked"] == ["0" * 24]
    assert second.exit_code == 0 and json.loads(second.stdout)["data"]["already_revoked"] is True

    by_owner = AsyncMock(return_value=["a" * 24, "b" * 24])
    monkeypatch.setattr(api_key_operations, "revoke_owner", by_owner)
    result = CliRunner().invoke(
        app,
        ["auth", "keys", "revoke", "--tenant", "engineering", "--owner", "user-huy", "--reason", "left"],
    )  # fmt: skip
    assert result.exit_code == 0
    assert json.loads(result.stdout)["data"]["revoked"] == ["a" * 24, "b" * 24]
    assert by_owner.call_args.kwargs["operator"] == "op@host"


def test_revoke_requires_exactly_one_selector():
    result = CliRunner().invoke(app, ["auth", "keys", "revoke", "--reason", "x"])
    assert result.exit_code != 0
    # Rich colours the usage error on CI (GITHUB_ACTIONS forces a terminal), which
    # splits "--key-id" with escape codes; compare the text a reader sees.
    assert "--key-id" in re.sub(r"\x1b\[[0-9;]*m", "", result.output)


def test_operation_errors_report_only_the_type(monkeypatch):
    monkeypatch.setattr(
        api_key_operations,
        "list_keys",
        AsyncMock(side_effect=RuntimeError("postgresql://user:hunter2@db/x")),
    )

    result = CliRunner().invoke(app, ["auth", "keys", "list", "--tenant", "t"])

    assert result.exit_code == 1
    assert "RuntimeError" in result.stderr
    assert "hunter2" not in result.stderr


def test_secret_file_is_created_exclusively(tmp_path: Path):
    target = tmp_path / "k.key"
    auth_keys.write_secret_file(target, "secret")

    assert stat.S_IMODE(os.stat(target).st_mode) == 0o600
    with pytest.raises(FileExistsError):
        auth_keys.write_secret_file(target, "again")


def test_an_absurd_duration_is_a_usage_error_not_a_traceback():
    # timedelta(days=99999999999) raises OverflowError; the bounded pattern
    # turns that into the same usage error as any other malformed value.
    with pytest.raises(Exception, match="d, h or m"):
        auth_keys.parse_duration("99999999999d")


def test_settings_failures_use_the_json_error_envelope(monkeypatch):
    from harborrag_runtime.config import settings as settings_module

    def broken(*args, **kwargs):
        raise ValueError("HARBORRAG_CONTROL_DB_URL=postgresql://u:hunter2@db/x is invalid")

    monkeypatch.setattr(settings_module, "RuntimeSettings", broken)

    result = CliRunner().invoke(app, ["auth", "keys", "list", "--tenant", "t"])

    assert result.exit_code == 1
    assert json.loads(result.stderr.strip().splitlines()[-1]) == {
        "ok": False,
        "error_code": "ValueError",
    }
    assert "hunter2" not in result.output


def test_create_defaults_to_a_private_key_directory_it_creates(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(api_key_operations, "ensure_key_schema", AsyncMock(return_value=False))
    monkeypatch.setattr(api_key_operations, "create_key", AsyncMock(return_value=_created()))
    monkeypatch.setattr(auth_keys, "operator_identity", lambda: "op@host")

    result = CliRunner().invoke(
        app,
        ["auth", "keys", "create", "--tenant", "t", "--owner", "user-huy",
         "--name", "huy-laptop", "--expires-in", "1d"],
    )  # fmt: skip

    assert result.exit_code == 0, result.stderr
    key_dir = tmp_path / ".harborrag" / "keys"
    key_file = key_dir / "huy-laptop.key"
    assert json.loads(result.stdout)["data"]["secret_written_to"] == str(key_file)
    assert stat.S_IMODE(key_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(key_file.stat().st_mode) == 0o600
    assert key_file.read_text(encoding="ascii").strip() == _created().raw_key


def test_create_makes_a_missing_secret_output_directory(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(api_key_operations, "ensure_key_schema", AsyncMock(return_value=False))
    monkeypatch.setattr(api_key_operations, "create_key", AsyncMock(return_value=_created()))
    output = tmp_path / "private" / "nested" / "huy-laptop.key"

    result = CliRunner().invoke(
        app,
        ["auth", "keys", "create", "--tenant", "t", "--owner", "user-huy", "--name", "k",
         "--expires-in", "1d", "--secret-output", str(output)],
    )  # fmt: skip

    assert result.exit_code == 0, result.stderr
    assert stat.S_IMODE(output.parent.stat().st_mode) == 0o700
    assert output.exists()


def test_create_migrate_flag_reaches_the_schema_check_before_issuing(tmp_path: Path, monkeypatch):
    calls: list[str] = []
    ensure = AsyncMock(side_effect=lambda *a, **k: calls.append("ensure") or True)
    create = AsyncMock(side_effect=lambda *a, **k: calls.append("create") or _created())
    monkeypatch.setattr(api_key_operations, "ensure_key_schema", ensure)
    monkeypatch.setattr(api_key_operations, "create_key", create)

    result = CliRunner().invoke(
        app,
        ["auth", "keys", "create", "--tenant", "t", "--owner", "user-huy", "--name", "k",
         "--expires-in", "1d", "--secret-output", str(tmp_path / "k.key"), "--migrate"],
    )  # fmt: skip

    assert result.exit_code == 0, result.stderr
    assert calls == ["ensure", "create"]
    assert ensure.call_args.kwargs == {"migrate": True}
    assert '"migrated": true' in result.stderr


def test_missing_schema_is_reported_with_the_fix(monkeypatch):
    from harborrag_runtime.security.api_key_operations import ApiKeySchemaMissing

    monkeypatch.setattr(
        api_key_operations, "ensure_key_schema", AsyncMock(side_effect=ApiKeySchemaMissing())
    )

    result = CliRunner().invoke(app, ["auth", "keys", "list", "--tenant", "t"])

    assert result.exit_code == 1
    payload = json.loads(result.stderr.strip().splitlines()[-1])
    assert payload["error_code"] == "SchemaMissing"
    assert "--migrate" in payload["detail"]


def test_settings_come_from_the_checkout_unless_exported(tmp_path: Path, monkeypatch):
    (tmp_path / "env").mkdir()
    (tmp_path / "env/.env.database").write_text(
        'POSTGRES_USER=owner\nPOSTGRES_PASSWORD="hunter2"\nPOSTGRES_DB=harbor\n', encoding="utf-8"
    )
    (tmp_path / "env/.env.api").write_text("HARBORRAG_ENV=prod\n", encoding="utf-8")
    monkeypatch.delenv("HARBORRAG_CONTROL_DB_URL", raising=False)
    monkeypatch.delenv("HARBORRAG_ENV", raising=False)

    assert auth_keys.settings_overrides(tmp_path) == {
        "control_db_url": "postgresql+asyncpg://owner:hunter2@localhost:5432/harbor",
        "env": "prod",
    }
    assert auth_keys.settings_overrides(None) == {}

    monkeypatch.setenv("HARBORRAG_CONTROL_DB_URL", "postgresql+asyncpg://x:y@db/z")
    monkeypatch.setenv("HARBORRAG_ENV", "dev")
    assert auth_keys.settings_overrides(tmp_path) == {}


def test_a_sqlite_fallback_is_warned_about(monkeypatch):
    monkeypatch.setattr("harborrag_app.cli.project.find_legacy_checkout", lambda *a, **k: None)
    monkeypatch.delenv("HARBORRAG_CONTROL_DB_URL", raising=False)
    monkeypatch.setattr(api_key_operations, "list_keys", AsyncMock(return_value=[]))

    result = CliRunner().invoke(app, ["auth", "keys", "list", "--tenant", "t"])

    assert result.exit_code == 0, result.stderr
    assert "local SQLite default" in result.stderr
    assert json.loads(result.stdout)["data"] == {"keys": []}


def test_policy_violations_show_the_rule(tmp_path: Path, monkeypatch):
    from harborrag_engine.security import ApiKeyPolicyError

    monkeypatch.setattr(
        api_key_operations,
        "create_key",
        AsyncMock(side_effect=ApiKeyPolicyError("lifetime must not exceed 7 days in dev")),
    )

    result = CliRunner().invoke(
        app,
        ["auth", "keys", "create", "--tenant", "t", "--owner", "user-huy", "--name", "k",
         "--expires-in", "30d", "--secret-output", str(tmp_path / "k.key")],
    )  # fmt: skip

    assert result.exit_code == 1
    payload = json.loads(result.stderr.strip().splitlines()[-1])
    assert payload == {
        "ok": False,
        "error_code": "ApiKeyPolicyError",
        "detail": "lifetime must not exceed 7 days in dev",
    }
    assert not (tmp_path / "k.key").exists()
