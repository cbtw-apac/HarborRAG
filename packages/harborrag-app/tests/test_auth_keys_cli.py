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


@pytest.fixture(autouse=True)
def _management_dsn(tmp_path: Path, monkeypatch) -> Path:
    """Every command refuses to start without a management DSN file."""

    dsn_file = tmp_path / "management.dsn"
    dsn_file.write_text("sqlite+aiosqlite:///:memory:\n", encoding="utf-8")
    dsn_file.chmod(0o600)  # the CLI refuses a DSN file other users can read
    monkeypatch.setenv(auth_keys.MANAGEMENT_DSN_FILE_ENV, str(dsn_file))
    monkeypatch.setattr("harborrag_app.cli.project.find_legacy_checkout", lambda *a, **k: None)
    return dsn_file


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
    expires = EXPIRES.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert result.stdout.splitlines() == [
        "Credential created.",
        "Key ID:   0123456789abcdef01234567",
        "Tenant:   engineering",
        "Owner:    user-huy",
        "Name:     huy-laptop",
        f"Expires:  {expires}",
        f"Secret written to {output} (mode 0600)",
    ]
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
        {
            "key_id": "0" * 24, "name": "laptop", "owner": "user-huy", "state": "active",
            "expires_at": "2026-12-01T00:00:00+00:00", "secret_hash": "should-not-appear",
        },
        {
            "key_id": "1" * 24, "name": "old", "owner": "user-huy", "state": "revoked",
            "expires_at": "2026-11-01T00:00:00+00:00", "secret_hash": "should-not-appear",
        },
    ]  # fmt: skip
    monkeypatch.setattr(api_key_operations, "list_keys", AsyncMock(return_value=rows))

    result = CliRunner().invoke(app, ["auth", "keys", "list", "--tenant", "engineering"])

    assert result.exit_code == 0
    lines = result.stdout.splitlines()
    assert [cell.strip() for cell in lines[0].split("|")] == [
        "KEY ID", "NAME", "OWNER", "STATE", "EXPIRES",
    ]  # fmt: skip
    assert "active" in lines[1] and "revoked" in lines[2]
    assert "should-not-appear" not in result.stdout


def test_list_with_no_keys_says_so(monkeypatch):
    monkeypatch.setattr(api_key_operations, "list_keys", AsyncMock(return_value=[]))

    result = CliRunner().invoke(app, ["auth", "keys", "list", "--tenant", "engineering"])

    assert result.exit_code == 0
    assert "no keys for tenant engineering" in result.stdout


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

    assert first.exit_code == 0 and first.stdout.strip() == f"revoked {'0' * 24}"
    assert second.exit_code == 0 and second.stdout.strip() == "already revoked"

    by_owner = AsyncMock(return_value=["a" * 24, "b" * 24])
    monkeypatch.setattr(api_key_operations, "revoke_owner", by_owner)
    result = CliRunner().invoke(
        app,
        ["auth", "keys", "revoke", "--tenant", "engineering", "--owner", "user-huy", "--reason", "left"],
    )  # fmt: skip
    assert result.exit_code == 0
    assert result.stdout.splitlines() == [f"revoked {'a' * 24}", f"revoked {'b' * 24}"]
    assert by_owner.call_args.kwargs["operator"] == "op@host"

    by_owner.return_value = []
    empty = CliRunner().invoke(
        app,
        ["auth", "keys", "revoke", "--tenant", "engineering", "--owner", "user-huy", "--reason", "left"],
    )  # fmt: skip
    assert empty.exit_code == 0 and empty.stdout.strip() == "already revoked"


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
    assert f"Secret written to {key_file} (mode 0600)" in result.stdout
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


def test_the_management_dsn_comes_from_the_file_and_beats_the_control_db_env(monkeypatch):
    monkeypatch.setenv("HARBORRAG_CONTROL_DB_URL", "postgresql+asyncpg://other:x@db/other")
    seen: dict[str, str] = {}

    async def fake_list(settings, *, operator, tenant_id):
        seen["dsn"] = settings.control_db_url.get_secret_value()
        return []

    monkeypatch.setattr(api_key_operations, "list_keys", fake_list)

    result = CliRunner().invoke(app, ["auth", "keys", "list", "--tenant", "t"])

    assert result.exit_code == 0, result.stderr
    assert seen["dsn"] == "sqlite+aiosqlite:///:memory:"


def test_a_missing_dsn_variable_refuses_to_start(tmp_path: Path, monkeypatch):
    monkeypatch.delenv(auth_keys.MANAGEMENT_DSN_FILE_ENV)
    create = AsyncMock(return_value=_created())
    monkeypatch.setattr(api_key_operations, "create_key", create)
    output = tmp_path / "private" / "k.key"

    result = CliRunner().invoke(
        app,
        ["auth", "keys", "create", "--tenant", "t", "--owner", "user-huy", "--name", "k",
         "--expires-in", "1d", "--secret-output", str(output)],
    )  # fmt: skip

    assert result.exit_code == 1
    assert json.loads(result.stderr.strip().splitlines()[-1])["error_code"] == (
        "ManagementDsnFileNotSet"
    )
    create.assert_not_called()
    assert not output.parent.exists()  # refused before any filesystem work


def test_an_empty_dsn_file_refuses_to_start(_management_dsn: Path):
    _management_dsn.write_text("  \n", encoding="utf-8")

    result = CliRunner().invoke(app, ["auth", "keys", "list", "--tenant", "t"])

    assert result.exit_code == 1
    assert json.loads(result.stderr.strip().splitlines()[-1])["error_code"] == "ManagementDsnEmpty"


@pytest.mark.skipif(os.name == "nt", reason="Windows reports a synthetic st_mode")
@pytest.mark.parametrize("mode", [0o640, 0o604, 0o644, 0o660])
def test_a_dsn_file_readable_by_other_users_refuses_to_start(_management_dsn: Path, mode: int):
    _management_dsn.chmod(mode)

    result = CliRunner().invoke(app, ["auth", "keys", "list", "--tenant", "t"])

    assert result.exit_code == 1
    payload = json.loads(result.stderr.strip().splitlines()[-1])
    assert payload["error_code"] == "ManagementDsnFileNotPrivate"
    assert "sqlite" not in result.stderr  # the credential is never echoed


def test_an_unreadable_dsn_file_reports_only_the_error_type(tmp_path: Path, monkeypatch):
    monkeypatch.setenv(auth_keys.MANAGEMENT_DSN_FILE_ENV, str(tmp_path / "absent.dsn"))

    result = CliRunner().invoke(app, ["auth", "keys", "list", "--tenant", "t"])

    assert result.exit_code == 1
    payload = json.loads(result.stderr.strip().splitlines()[-1])
    assert payload == {
        "ok": False,
        "error_code": "ManagementDsnFileUnreadable",
        "detail": "FileNotFoundError",
    }
    assert "absent.dsn" not in result.stderr


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


def test_an_unknown_tenant_is_reported_by_its_own_code(tmp_path: Path, monkeypatch):
    from harborrag_engine.security import TenantNotFound

    message = "tenant_id names no known tenant; submit an ingestion for it first"
    monkeypatch.setattr(
        api_key_operations, "create_key", AsyncMock(side_effect=TenantNotFound(message))
    )

    result = CliRunner().invoke(
        app,
        ["auth", "keys", "create", "--tenant", "typo", "--owner", "user-huy", "--name", "k",
         "--expires-in", "1d", "--secret-output", str(tmp_path / "k.key")],
    )  # fmt: skip

    assert result.exit_code == 1
    payload = json.loads(result.stderr.strip().splitlines()[-1])
    assert payload == {"ok": False, "error_code": "TenantNotFound", "detail": message}
    assert not (tmp_path / "k.key").exists()
