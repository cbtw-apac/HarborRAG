"""Durable, transport-independent reader-tool execution audit."""

from __future__ import annotations

import json
import os
import stat
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from harborrag_core.contracts.tools import ToolInvocationContext


@dataclass(slots=True)
class JsonlToolExecutionAudit:
    path: Path
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def __post_init__(self) -> None:
        if not self.path.is_absolute():
            self.path = Path.home() / self.path

    async def record(self, context: ToolInvocationContext, name: str, outcome: str) -> None:
        event = {
            "event": "tool_execution_completed",
            "invocation_id": context.invocation_id or uuid4().hex,
            "tool": name[:128],
            "tenant_id": str(context.access.tenant_id)[:128],
            "principal_id": context.access.principal_id[:128],
            "outcome": outcome,
            "timestamp": datetime.now(UTC).isoformat(),
        }
        self._append(event)

    def _append(self, event: dict[str, str]) -> None:
        path = self.path
        with self._lock:
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            if path.parent.is_symlink():
                raise OSError("tool audit directory must not be a symlink")
            descriptor = os.open(
                path,
                os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            try:
                metadata = os.fstat(descriptor)
                if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                    raise OSError("tool audit path must be a single-link regular file")
                if hasattr(os, "getuid") and metadata.st_uid != os.getuid():
                    raise PermissionError("tool audit file must be owned by this process user")
                value = (json.dumps(event, separators=(",", ":")) + "\n").encode()
                while value:
                    written = os.write(descriptor, value)
                    if written <= 0:
                        raise OSError("tool audit write made no progress")
                    value = value[written:]
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
