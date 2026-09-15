"""Summary administration is explicit and cleanup previews by default."""

import json
from unittest.mock import AsyncMock

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
