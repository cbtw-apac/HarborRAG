"""Process-local defaults for MCP server tests."""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolated_audit_log(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Keep CLI audit output inside each test's temporary directory."""

    audit_path = tmp_path / "mcp-audit.jsonl"
    monkeypatch.setenv("HARBORRAG_MCP_AUDIT_PATH", str(audit_path))

    # Test modules import the server during collection, before fixtures run, so
    # changing the environment alone cannot affect its process-wide singleton.
    import harborrag_mcp_server.server.server as server_module
    from harborrag_mcp_server.audit import McpAuditLog

    monkeypatch.setattr(server_module, "_default_audit_log", McpAuditLog(path=audit_path))
