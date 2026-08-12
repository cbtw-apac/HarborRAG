from __future__ import annotations

import json
import os
import stat
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

    def configuration_change(
        self,
        *,
        action: str,
        principal_id: str,
        previous_revision: str,
        current_revision: str,
    ) -> None:
        """Record configuration metadata without persisting configuration values."""
        self._record(
            {
                "event": "configuration_changed",
                "action": _bounded(action),
                "principal_id": _bounded(principal_id),
                "previous_revision": _bounded(previous_revision),
                "current_revision": _bounded(current_revision),
                "timestamp": datetime.now(UTC).isoformat(),
            }
        )

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
        descriptor = _open_audit_file(path)
        try:
            value = memoryview((json.dumps(event, separators=(",", ":")) + "\n").encode("utf-8"))
            while value:
                written = os.write(descriptor, value)
                if written <= 0:
                    raise OSError("durable MCP audit write made no progress")
                value = value[written:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


_SUPPORTS_DIR_FD = os.open in os.supports_dir_fd


def _open_audit_file(path: Path) -> int:
    """Open the durable audit file with the strongest hardening the platform supports."""

    if not _SUPPORTS_DIR_FD:
        return _open_audit_file_fallback(path)
    return _open_audit_file_dir_fd(path)


def _open_audit_file_fallback(path: Path) -> int:
    """Best-effort open for platforms without `dir_fd`/`openat()` support (Windows).

    Windows has no O_DIRECTORY/O_NOFOLLOW-per-component equivalent, so this
    narrows rather than closes the TOCTOU window `_open_audit_file_dir_fd`
    closes on POSIX: the parent directory and final path are checked for
    symlinks immediately before opening, then the opened descriptor is
    re-checked against a fresh `lstat` of the same path.
    """

    if path.parent.is_symlink():
        raise OSError("durable MCP audit directory must not be a symbolic link")
    if path.is_symlink():
        raise OSError("durable MCP audit path must not be a symbolic link")

    file_flags = (
        os.O_WRONLY
        | os.O_APPEND
        | os.O_CREAT
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor = os.open(path, file_flags, 0o600)
    try:
        metadata = os.fstat(descriptor)
        named_metadata = os.lstat(path)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_ISLNK(named_metadata.st_mode)
            or _identity(metadata) != _identity(named_metadata)
            or metadata.st_nlink != 1
        ):
            raise OSError("durable MCP audit path must be a single-link regular file")
        if not _owned_by_current_process(metadata):
            raise PermissionError("durable MCP audit file must be owned by this process user")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _open_audit_file_dir_fd(path: Path) -> int:
    """Open an owner-only regular file without following the final directory entries."""

    directory_flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    directory_descriptor = os.open(path.parent, directory_flags)
    try:
        directory_metadata = os.fstat(directory_descriptor)
        named_directory_metadata = os.lstat(path.parent)
        if (
            not stat.S_ISDIR(directory_metadata.st_mode)
            or stat.S_ISLNK(named_directory_metadata.st_mode)
            or _identity(directory_metadata) != _identity(named_directory_metadata)
        ):
            raise OSError("durable MCP audit directory must not be a symbolic link")
        if stat.S_IMODE(directory_metadata.st_mode) & 0o077:
            raise PermissionError("durable MCP audit directory must be owner-only")
        if not _owned_by_current_process(directory_metadata):
            raise PermissionError("durable MCP audit directory must be owned by this process user")

        file_flags = (
            os.O_WRONLY
            | os.O_APPEND
            | os.O_CREAT
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0)
        )
        descriptor = os.open(
            path.name,
            file_flags,
            0o600,
            dir_fd=directory_descriptor,
        )
        try:
            metadata = os.fstat(descriptor)
            named_metadata = os.stat(
                path.name,
                dir_fd=directory_descriptor,
                follow_symlinks=False,
            )
            if (
                not stat.S_ISREG(metadata.st_mode)
                or stat.S_ISLNK(named_metadata.st_mode)
                or _identity(metadata) != _identity(named_metadata)
                or metadata.st_nlink != 1
            ):
                raise OSError("durable MCP audit path must be a single-link regular file")
            if not _owned_by_current_process(metadata):
                raise PermissionError("durable MCP audit file must be owned by this process user")
            os.fchmod(descriptor, 0o600)
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise
    finally:
        os.close(directory_descriptor)


def _identity(metadata: os.stat_result) -> tuple[int, int]:
    return metadata.st_dev, metadata.st_ino


def _owned_by_current_process(metadata: os.stat_result) -> bool:
    effective_user_id = getattr(os, "geteuid", None)
    return effective_user_id is None or metadata.st_uid == effective_user_id()


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
