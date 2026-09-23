"""Runtime selection of the shared reader execution audit writer."""

from __future__ import annotations

import os
from pathlib import Path

from harborrag_adapters.repositories.tool_audit import JsonlToolExecutionAudit


def build_tool_execution_audit() -> JsonlToolExecutionAudit:
    path = Path(
        os.environ.get(
            "HARBORRAG_TOOL_EXECUTION_AUDIT_PATH", ".harborrag/tool-execution-audit.jsonl"
        )
    )
    return JsonlToolExecutionAudit(path)
