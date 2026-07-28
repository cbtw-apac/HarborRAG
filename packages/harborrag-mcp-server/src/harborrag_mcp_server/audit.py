from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from uuid import uuid4


@dataclass(slots=True)
class McpAuditLog:
    """Bounded process trail plus optional durable owner-only JSONL log."""

    path: Path | None = None
    max_entries: int = 1_000
    entries: list[dict[str, object]] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.max_entries < 1:
            raise ValueError("MCP audit max_entries must be positive")

    def start(
        self,
        tool: str,
        arguments: dict[str, object],
        *,
        principal_id: str,
    ) -> str:
        invocation_id = uuid4().hex
        self._record(
            {
                "invocation_id": invocation_id,
                "tool": _bounded(tool),
                "principal_id": _bounded(principal_id),
                "arguments_sha256": _arguments_digest(arguments),
                "event": "tool_invocation_attempted",
                "timestamp": datetime.now(UTC).isoformat(),
            }
        )
        return invocation_id

    def finish(
        self,
        invocation_id: str,
        tool: str,
        *,
        principal_id: str,
        outcome: str,
        error_type: str | None = None,
    ) -> None:
        event: dict[str, object] = {
            "invocation_id": invocation_id,
            "tool": _bounded(tool),
            "principal_id": _bounded(principal_id),
            "event": "tool_invocation_completed",
            "timestamp": datetime.now(UTC).isoformat(),
            "outcome": outcome,
        }
        if error_type is not None:
            event["error_type"] = _bounded(error_type)
        self._record(event)

    def _record(self, event: dict[str, object]) -> None:
        with self._lock:
            self.entries.append(event)
            if len(self.entries) > self.max_entries:
                del self.entries[: len(self.entries) - self.max_entries]
            if self.path is not None:
                self._append(event)

    def _append(self, event: dict[str, object]) -> None:
        path = self.path
        if path is None:
            raise RuntimeError("durable MCP audit path is not configured")
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(path.parent, 0o700)
        descriptor = os.open(
            path,
            os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_CLOEXEC", 0),
            0o600,
        )
        try:
            os.write(
                descriptor,
                (json.dumps(event, separators=(",", ":")) + "\n").encode("utf-8"),
            )
        finally:
            os.close(descriptor)


def _arguments_digest(arguments: dict[str, object]) -> str:
    try:
        value = json.dumps(
            arguments,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError):
        value = b"non-json-arguments"
    return sha256(value).hexdigest()


def _bounded(value: str) -> str:
    return value[:256] if value else "unknown"
