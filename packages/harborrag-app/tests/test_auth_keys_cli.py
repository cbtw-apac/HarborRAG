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


def _error(result) -> dict:
    """The ``error`` object of the common envelope; stderr holds nothing else."""

    return json.loads(result.stderr)["error"]


def _created() -> CreatedKey:
    return CreatedKey(
        key_id="0123456789abcdef01234567",
        tenant_id="engineering",
        owner="user-demo",
        name="demo-laptop",
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
    output = tmp_path / "demo-laptop.key"

    result = CliRunner().invoke(
        app,
        [
            "auth", "keys", "create",
            "--tenant", "engineering",
            "--owner", "user-demo",
            "--name", "demo-laptop",
            "--expires-in", "30d",
            "--secret-output", str(output),
        ],
    )  # fmt: skip

    assert result.exit_code == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["key_id"] == "0123456789abcdef01234567"
    assert "ok" not in payload and "data" not in payload
    assert "hrk_" not in result.stdout
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    written = output.read_text(encoding="ascii").strip()
    assert parse_key(written) is not None and written == _created().raw_key
    assert create.call_args.kwargs == {
        "operator": "op@host",
        "tenant_id": "engineering",
        "owner": "user-demo",
        "name": "demo-laptop",
        "lifetime": timedelta(days=30),
    }


@pytest.mark.parametrize("output_format", ["table", "yaml", "json"])
def test_create_never_prints_the_secret_in_any_format(tmp_path: Path, monkeypatch, output_format):
    monkeypatch.setattr(api_key_operations, "create_key", AsyncMock(return_value=_created()))
    output = tmp_path / "demo-laptop.key"

    result = CliRunner().invoke(
        app,
        ["auth", "keys", "create", "--tenant", "engineering", "--owner", "user-demo",
         "--name", "demo-laptop", "--expires-in", "30d", "--secret-output", str(output),
         "-o", output_format],
    )  # fmt: skip

    assert result.exit_code == 0, result.stderr
    assert "hrk_" not in result.stdout
    assert "hrk_" not in result.stderr
    assert output.read_text(encoding="ascii").strip() == _created().raw_key


def test_a_failure_leaves_stderr_as_the_error_envelope_alone(monkeypatch):
    from harborrag_engine.security import ApiKeyPolicyError

    monkeypatch.setattr("harborrag_app.cli.project.find_legacy_checkout", lambda *a, **k: None)
    monkeypatch.delenv("HARBORRAG_CONTROL_DB_URL", raising=False)
    monkeypatch.delenv("HARBORRAG_LOG_LEVEL", raising=False)
    monkeypatch.setattr(
        api_key_operations, "list_keys", AsyncMock(side_effect=ApiKeyPolicyError("rejected"))
    )

    result = CliRunner().invoke(app, ["auth", "keys", "list", "--tenant", "t", "-o", "json"])

    assert result.exit_code == 2
    assert _error(result)["code"] == "validation_error"
    assert "SQLite" not in result.stderr


def test_a_failed_migration_run_leaves_stderr_as_the_error_envelope_alone(
    tmp_path: Path, monkeypatch
):
    from harborrag_engine.security import ApiKeyPolicyError

    monkeypatch.delenv("HARBORRAG_LOG_LEVEL", raising=False)
    monkeypatch.setattr(api_key_operations, "ensure_key_schema", AsyncMock(return_value=True))
    monkeypatch.setattr(
        api_key_operations, "create_key", AsyncMock(side_effect=ApiKeyPolicyError("rejected"))
    )

    result = CliRunner().invoke(
        app,
        ["auth", "keys", "create", "--tenant", "t", "--owner", "user-demo", "--name", "k",
         "--expires-in", "1d", "--secret-output", str(tmp_path / "k.key"), "--migrate",
         "-o", "json"],
    )  # fmt: skip

    assert result.exit_code == 2
    assert _error(result)["code"] == "validation_error"


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
    assert _error(result)["details"] == {"key_id": "0123456789abcdef01234567"}
    assert "hrk_" not in result.output


def test_list_delegates_and_shows_no_secret_material(monkeypatch):
    rows = [
        {"key_id": "0" * 24, "name": "laptop", "owner": "user-demo", "state": "active"},
        {"key_id": "1" * 24, "name": "old", "owner": "user-demo", "state": "revoked"},
    ]
    monkeypatch.setattr(api_key_operations, "list_keys", AsyncMock(return_value=rows))

    result = CliRunner().invoke(app, ["auth", "keys", "list", "--tenant", "engineering"])

    assert result.exit_code == 0
    assert json.loads(result.stdout)["keys"] == rows
    assert "secret_hash" not in result.stdout


def test_revoke_by_id_is_idempotent_and_by_owner_reports_ids(monkeypatch):
    monkeypatch.setattr(auth_keys, "operator_identity", lambda: "op@host")
    revoke = AsyncMock(side_effect=["revoked", "already_revoked"])
    monkeypatch.setattr(api_key_operations, "revoke_key", revoke)

    first = CliRunner().invoke(
        app, ["auth", "keys", "revoke", "--key-id", "0" * 24, "--reason", "lost"]
    )
    second = CliRunner().invoke(
        app, ["auth", "keys", "revoke", "--key-id", "0" * 24, "--reason", "lost"]
    )

    assert first.exit_code == 0 and json.loads(first.stdout)["revoked"] == ["0" * 24]
    assert second.exit_code == 0 and json.loads(second.stdout)["already_revoked"] is True

    by_owner = AsyncMock(return_value=["a" * 24, "b" * 24])
    monkeypatch.setattr(api_key_operations, "revoke_owner", by_owner)
    result = CliRunner().invoke(
        app,
        ["auth", "keys", "revoke", "--tenant", "engineering", "--owner", "user-demo", "--reason", "left"],
    )  # fmt: skip
    assert result.exit_code == 0
    # Same shape as the empty case: the revoked ids and how many active keys they were.
    assert json.loads(result.stdout) == {"revoked": ["a" * 24, "b" * 24], "active_keys": 2}
    assert by_owner.call_args.kwargs["operator"] == "op@host"


def test_revoke_owner_needs_an_explicit_tenant_whatever_the_environment_says(monkeypatch):
    monkeypatch.setenv("HARBORRAG_TENANT_ID", "engineering")
    by_owner = AsyncMock(return_value=[])
    monkeypatch.setattr(api_key_operations, "revoke_owner", by_owner)

    result = CliRunner().invoke(
        app, ["auth", "keys", "revoke", "--owner", "user-demo", "--reason", "left"]
    )

    assert result.exit_code != 0
    assert "explicit --tenant" in re.sub(r"\x1b\[[0-9;]*m", "", result.output)
    by_owner.assert_not_called()


def test_revoke_help_says_the_tenant_is_explicit():
    result = CliRunner().invoke(app, ["auth", "keys", "revoke", "--help"])

    text = " ".join(re.sub(r"\x1b\[[0-9;]*m", "", result.stdout).split())
    assert "Required with --owner" in text
    assert "default: the profile" not in text


def test_revoke_requires_exactly_one_selector():
    result = CliRunner().invoke(app, ["auth", "keys", "revoke", "--reason", "x"])
    assert result.exit_code != 0
    # Rich colours the usage error on CI (GITHUB_ACTIONS forces a terminal), which
    # splits "--key-id" with escape codes; compare the text a reader sees.
    assert "--key-id" in re.sub(r"\x1b\[[0-9;]*m", "", result.output)


def test_unexpected_errors_are_reported_without_their_text(monkeypatch):
    monkeypatch.setattr(
        api_key_operations,
        "list_keys",
        AsyncMock(side_effect=RuntimeError("postgresql://user:hunter2@db/x")),
    )

    result = CliRunner().invoke(app, ["auth", "keys", "list", "--tenant", "t"])

    assert result.exit_code == 1
    assert _error(result)["code"] == "internal_error"
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


def test_settings_failures_use_the_common_error_envelope(monkeypatch):
    from harborrag_runtime.config import settings as settings_module

    def broken(*args, **kwargs):
        raise ValueError("HARBORRAG_CONTROL_DB_URL=postgresql://u:hunter2@db/x is invalid")

    monkeypatch.setattr(settings_module, "RuntimeSettings", broken)

    result = CliRunner().invoke(app, ["auth", "keys", "list", "--tenant", "t"])

    assert result.exit_code == 1
    assert _error(result)["code"] == "internal_error"
    assert "hunter2" not in result.output


def test_create_defaults_to_a_private_key_directory_it_creates(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(api_key_operations, "ensure_key_schema", AsyncMock(return_value=False))
    monkeypatch.setattr(api_key_operations, "create_key", AsyncMock(return_value=_created()))
    monkeypatch.setattr(auth_keys, "operator_identity", lambda: "op@host")

    result = CliRunner().invoke(
        app,
        ["auth", "keys", "create", "--tenant", "t", "--owner", "user-demo",
         "--name", "demo-laptop", "--expires-in", "1d"],
    )  # fmt: skip

    assert result.exit_code == 0, result.stderr
    key_dir = tmp_path / ".harborrag" / "keys"
    key_file = key_dir / "demo-laptop.key"
    assert json.loads(result.stdout)["secret_written_to"] == str(key_file)
    assert stat.S_IMODE(key_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(key_file.stat().st_mode) == 0o600
    assert key_file.read_text(encoding="ascii").strip() == _created().raw_key


def test_create_makes_a_missing_secret_output_directory(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(api_key_operations, "ensure_key_schema", AsyncMock(return_value=False))
    monkeypatch.setattr(api_key_operations, "create_key", AsyncMock(return_value=_created()))
    output = tmp_path / "private" / "nested" / "demo-laptop.key"

    result = CliRunner().invoke(
        app,
        ["auth", "keys", "create", "--tenant", "t", "--owner", "user-demo", "--name", "k",
         "--expires-in", "1d", "--secret-output", str(output)],
    )  # fmt: skip

    assert result.exit_code == 0, result.stderr
    assert stat.S_IMODE(output.parent.stat().st_mode) == 0o700
    assert output.exists()


def test_create_migrate_flag_reaches_the_schema_check_before_issuing(
    tmp_path: Path, monkeypatch, caplog
):
    calls: list[str] = []
    ensure = AsyncMock(side_effect=lambda *a, **k: calls.append("ensure") or True)
    create = AsyncMock(side_effect=lambda *a, **k: calls.append("create") or _created())
    monkeypatch.setattr(api_key_operations, "ensure_key_schema", ensure)
    monkeypatch.setattr(api_key_operations, "create_key", create)

    result = CliRunner().invoke(
        app,
        ["auth", "keys", "create", "--tenant", "t", "--owner", "user-demo", "--name", "k",
         "--expires-in", "1d", "--secret-output", str(tmp_path / "k.key"), "--migrate"],
    )  # fmt: skip

    assert result.exit_code == 0, result.stderr
    assert calls == ["ensure", "create"]
    assert ensure.call_args.kwargs == {"migrate": True}
    assert "applied the pending control-plane migrations" in caplog.text


def test_missing_schema_is_reported_with_the_fix(monkeypatch):
    from harborrag_runtime.security.api_key_operations import ApiKeySchemaMissing

    monkeypatch.setattr(
        api_key_operations, "ensure_key_schema", AsyncMock(side_effect=ApiKeySchemaMissing())
    )

    result = CliRunner().invoke(app, ["auth", "keys", "list", "--tenant", "t"])

    assert result.exit_code == 6
    error = _error(result)
    assert error["code"] == "not_ready"
    assert "--migrate" in error["message"]


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


def test_a_sqlite_fallback_is_warned_about(monkeypatch, caplog):
    monkeypatch.setattr("harborrag_app.cli.project.find_legacy_checkout", lambda *a, **k: None)
    monkeypatch.delenv("HARBORRAG_CONTROL_DB_URL", raising=False)
    monkeypatch.setattr(api_key_operations, "list_keys", AsyncMock(return_value=[]))

    result = CliRunner().invoke(app, ["auth", "keys", "list", "--tenant", "t"])

    assert result.exit_code == 0, result.stderr
    assert "local SQLite default" in caplog.text
    assert json.loads(result.stdout) == {"keys": []}


def test_policy_violations_show_the_rule(tmp_path: Path, monkeypatch):
    from harborrag_engine.security import ApiKeyPolicyError

    monkeypatch.setattr(
        api_key_operations,
        "create_key",
        AsyncMock(side_effect=ApiKeyPolicyError("lifetime must not exceed 7 days in dev")),
    )

    result = CliRunner().invoke(
        app,
        ["auth", "keys", "create", "--tenant", "t", "--owner", "user-demo", "--name", "k",
         "--expires-in", "30d", "--secret-output", str(tmp_path / "k.key")],
    )  # fmt: skip

    assert result.exit_code == 2
    error = _error(result)
    assert error["code"] == "validation_error"
    assert error["message"] == "lifetime must not exceed 7 days in dev"
    assert not (tmp_path / "k.key").exists()


def test_constraint_rejection_is_explained_without_values(monkeypatch):
    from sqlalchemy.exc import IntegrityError

    from harborrag_runtime.security.api_key_operations import ApiKeyRejected

    # The runtime wraps IntegrityError; the CLI must also survive a raw one.
    monkeypatch.setattr(
        api_key_operations,
        "list_keys",
        AsyncMock(side_effect=ApiKeyRejected()),
    )
    wrapped = CliRunner().invoke(app, ["auth", "keys", "list", "--tenant", "t"])
    monkeypatch.setattr(
        api_key_operations,
        "list_keys",
        AsyncMock(
            side_effect=IntegrityError(
                "INSERT ... hunter2", {"secret_hash": "hunter2"}, Exception("hunter2")
            )
        ),
    )
    raw = CliRunner().invoke(app, ["auth", "keys", "list", "--tenant", "t"])

    for result in (wrapped, raw):
        assert result.exit_code == 4
        error = _error(result)
        assert error["code"] == "invalid_state"
        assert "--migrate" in error["message"]
        assert "hunter2" not in result.output


def test_unstamped_schema_is_reported_with_the_fix(monkeypatch):
    from harborrag_runtime.security.api_key_operations import ApiKeySchemaUnstamped

    monkeypatch.setattr(
        api_key_operations,
        "ensure_key_schema",
        AsyncMock(side_effect=ApiKeySchemaUnstamped("sqlite:///x")),
    )

    result = CliRunner().invoke(
        app,
        ["auth", "keys", "create", "--tenant", "t", "--owner", "user-x", "--name", "k",
         "--expires-in", "1d", "--migrate"],
    )  # fmt: skip

    error = _error(result)
    assert result.exit_code == 6
    assert error["code"] == "not_ready"
    assert "no migration stamp" in error["message"] and "alembic" in error["message"]


def test_schema_behind_head_is_reported_with_the_fix(monkeypatch):
    from harborrag_runtime.security.api_key_operations import ApiKeySchemaBehind

    monkeypatch.setattr(
        api_key_operations,
        "ensure_key_schema",
        AsyncMock(side_effect=ApiKeySchemaBehind("sqlite:///x", "0039", "0040")),
    )

    result = CliRunner().invoke(
        app,
        ["auth", "keys", "create", "--tenant", "t", "--owner", "user-x", "--name", "k",
         "--expires-in", "1d"],
    )  # fmt: skip

    error = _error(result)
    assert result.exit_code == 6
    assert error["code"] == "not_ready"
    assert "0039" in error["message"] and "--migrate" in error["message"]


def test_an_unavailable_key_store_is_named_without_its_cause(monkeypatch):
    from harborrag_core.ports.api_keys import AuthStoreUnavailable

    monkeypatch.setattr(
        api_key_operations,
        "list_keys",
        AsyncMock(side_effect=AuthStoreUnavailable("MCP key store is unavailable")),
    )

    result = CliRunner().invoke(app, ["auth", "keys", "list", "--tenant", "t"])

    assert result.exit_code == 6
    error = _error(result)
    assert (error["code"], error["message"]) == (
        "dependency_unavailable",
        "MCP key store is unavailable",
    )


def test_revoking_an_unknown_key_is_not_found(monkeypatch):
    monkeypatch.setattr(api_key_operations, "revoke_key", AsyncMock(return_value="not_found"))

    result = CliRunner().invoke(
        app, ["auth", "keys", "revoke", "--key-id", "f" * 24, "--reason", "lost"]
    )

    assert result.exit_code == 3
    assert _error(result)["code"] == "not_found"


def test_revoke_by_owner_with_only_expired_keys_says_so(monkeypatch):
    monkeypatch.setattr(api_key_operations, "revoke_owner", AsyncMock(return_value=[]))

    result = CliRunner().invoke(
        app,
        ["auth", "keys", "revoke", "--tenant", "t", "--owner", "user-demo", "--reason", "left"],
    )

    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert data["revoked"] == [] and data["active_keys"] == 0
    assert "already_revoked" not in data


def test_settings_validation_detail_names_the_variable(monkeypatch):
    # A real validation failure: names the variable, never the value.
    monkeypatch.setenv("HARBORRAG_ENV", "staging")
    monkeypatch.setenv("HARBORRAG_CONTROL_DB_URL", "sqlite+aiosqlite:///hunter2.db")

    result = CliRunner().invoke(app, ["auth", "keys", "list", "--tenant", "t"])

    assert result.exit_code == 1
    error = _error(result)
    assert "HARBORRAG_" in error["message"]
    assert "hunter2" not in result.output


def test_list_honours_the_output_option(monkeypatch):
    rows = [{"key_id": "0" * 24, "name": "laptop", "owner": "user-demo", "state": "active"}]
    monkeypatch.setattr(api_key_operations, "list_keys", AsyncMock(return_value=rows))

    as_yaml = CliRunner().invoke(app, ["auth", "keys", "list", "-o", "yaml"])
    as_table = CliRunner().invoke(app, ["auth", "keys", "list", "-o", "table"])

    assert as_yaml.exit_code == 0 and as_table.exit_code == 0
    assert "keys:" in as_yaml.stdout and "key_id" in as_yaml.stdout
    assert "laptop" in as_table.stdout and not as_table.stdout.lstrip().startswith("{")


def test_tenant_defaults_like_the_resource_commands(tmp_path: Path, monkeypatch):
    listing = AsyncMock(return_value=[])
    create = AsyncMock(return_value=_created())
    monkeypatch.setattr(api_key_operations, "list_keys", listing)
    monkeypatch.setattr(api_key_operations, "create_key", create)
    monkeypatch.delenv("HARBORRAG_TENANT_ID", raising=False)
    create_args = [
        "auth",
        "keys",
        "create",
        "--owner",
        "user-demo",
        "--name",
        "k",
        "--expires-in",
        "1d",
    ]

    CliRunner().invoke(app, ["auth", "keys", "list"])
    CliRunner().invoke(app, [*create_args, "--secret-output", str(tmp_path / "a.key")])
    monkeypatch.setenv("HARBORRAG_TENANT_ID", "engineering")
    CliRunner().invoke(app, ["auth", "keys", "list"])
    CliRunner().invoke(app, [*create_args, "--secret-output", str(tmp_path / "b.key")])

    assert [call.args[1] for call in listing.call_args_list] == ["DEFAULT", "engineering"]
    assert [call.kwargs["tenant_id"] for call in create.call_args_list] == [
        "DEFAULT",
        "engineering",
    ]
