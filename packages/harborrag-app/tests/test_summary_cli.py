"""Summary administration is explicit and cleanup previews by default."""

import json
from unittest.mock import AsyncMock

import pytest
from typer.testing import CliRunner

from harborrag_app.cli.main import app
from harborrag_runtime.topology import summary_operations


def test_summary_help_exposes_worker_and_recovery_commands():
    result = CliRunner().invoke(app, ["topology", "summaries", "--help"])
    assert result.exit_code == 0
    for name in ("status", "backfill", "run-once", "worker", "cleanup"):
        assert name in result.stdout


def test_cleanup_is_dry_run_unless_explicitly_applied(monkeypatch):
    cleanup = AsyncMock(return_value={"removed": 0})
    monkeypatch.setattr(summary_operations, "cleanup", cleanup)
    result = CliRunner().invoke(app, ["topology", "summaries", "cleanup", "--tenant", "demo"])
    assert result.exit_code == 0
    assert json.loads(result.stdout)["ok"]
    assert cleanup.call_args.args[1] == "demo"
    assert cleanup.call_args.kwargs == {"retention_days": 30, "apply": False}


@pytest.mark.parametrize(
    ("command", "operation"),
    [
        (["status"], "status"),
        (["backfill", "--source-scope", "docs"], "backfill"),
        (["run-once"], "run_once"),
        (["worker"], "worker"),
    ],
)
def test_summary_commands_delegate_and_emit_json(monkeypatch, command, operation):
    call = AsyncMock(return_value={"state": "ok"})
    monkeypatch.setattr(summary_operations, operation, call)
    result = CliRunner().invoke(app, ["topology", "summaries", *command, "--tenant", "demo"])
    assert result.exit_code == 0
    assert json.loads(result.stdout)["data"] == {"state": "ok"}


def test_summary_command_reports_safe_error_code(monkeypatch):
    monkeypatch.setattr(
        summary_operations,
        "status",
        AsyncMock(side_effect=RuntimeError("private provider detail")),
    )
    result = CliRunner().invoke(app, ["topology", "summaries", "status", "--tenant", "demo"])
    assert result.exit_code == 1
    assert "RuntimeError" in result.stderr
    assert "private provider detail" not in result.stderr
