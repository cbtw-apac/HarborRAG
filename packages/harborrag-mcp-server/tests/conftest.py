"""Process-local defaults for MCP server tests."""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolated_audit_log(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Keep CLI audit output inside each test's temporary directory."""

    monkeypatch.setenv("HARBORRAG_MCP_AUDIT_PATH", str(tmp_path / "mcp-audit.jsonl"))
