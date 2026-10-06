"""Shared execution audit does not persist model arguments."""

from __future__ import annotations

import json
import os

import pytest

from harborrag_adapters.repositories.tool_audit import JsonlToolExecutionAudit
from harborrag_core.contracts.tools import ToolInvocationContext
from harborrag_core.security import AccessContext


@pytest.mark.asyncio
@pytest.mark.skipif(
    os.name == "nt",
    reason="Fails on Windows due to cv2 import issue",
)
async def test_execution_audit_records_identity_and_correlation_without_arguments(tmp_path) -> None:
    path = tmp_path / "tool-audit.jsonl"
    writer = JsonlToolExecutionAudit(path)
    context = ToolInvocationContext(
        AccessContext(principal_id="reader", tenant_id="acme"), invocation_id="call-1"
    )

    await writer.record(context, "vector_search", "success")

    record = json.loads(path.read_text())
    assert record["invocation_id"] == "call-1"
    assert record["tenant_id"] == "acme"
    assert record["principal_id"] == "reader"
    assert record["outcome"] == "success"
    assert "arguments" not in record
    assert path.stat().st_mode & 0o077 == 0
