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
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        return _open_audit_file_fallback(path)
    return _open_audit_file_dir_fd(path)


_REPARSE_POINT_ATTRIBUTE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


def _is_reparse_point(metadata: os.stat_result) -> bool:
    """Return whether an `lstat()` result names a symlink, junction, or other reparse point.

    `Path.is_symlink()` only recognizes the `IO_REPARSE_TAG_SYMLINK` tag.
    NTFS junctions (`IO_REPARSE_TAG_MOUNT_POINT`) and other reparse points
    report `is_symlink() == False` while still redirecting file operations to
    an arbitrary target, so every check here also inspects the
    `FILE_ATTRIBUTE_REPARSE_POINT` bit, which `lstat()` reports without
    following the reparse point.
    """
    return bool(stat.S_ISLNK(metadata.st_mode)) or bool(
        getattr(metadata, "st_file_attributes", 0) & _REPARSE_POINT_ATTRIBUTE
    )


def _reject_reparse_component(component: Path) -> None:
    try:
        metadata = os.lstat(component)
    except FileNotFoundError:
        return
    if _is_reparse_point(metadata):
        raise OSError(
            f"durable MCP audit path must not contain a symlink or reparse point: {component}"
        )


def _open_audit_file_fallback(path: Path) -> int:
    """Best-effort open for platforms without `dir_fd`/`openat()` support (Windows).

    Windows has no O_DIRECTORY/O_NOFOLLOW-per-component equivalent, so this
    narrows rather than closes the TOCTOU window `_open_audit_file_dir_fd`
    closes on POSIX: every path component -- including `path.parent` and
    `path` -- is checked for a symlink or reparse point immediately before
    opening, `path.parent` is re-checked right after `os.open()` to narrow
    the window where it could be replaced by a junction between the
    pre-open check and the open call, and the opened descriptor is finally
    re-checked against a fresh `lstat` of the same path.
    """

    for component in (*reversed(path.parents), path):
        _reject_reparse_component(component)

    file_flags = (
        os.O_WRONLY
        | os.O_APPEND
        | os.O_CREAT
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor = os.open(path, file_flags, 0o600)
    try:
        _reject_reparse_component(path.parent)
        metadata = os.fstat(descriptor)
        named_metadata = os.lstat(path)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or _is_reparse_point(named_metadata)
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


_DIRECTORY_OPEN_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_NOFOLLOW", 0)
)


def _open_validated_parent_dir_fd(path: Path) -> int:
    """Return a held descriptor for `path.parent`, validating every ancestor on the way.

    Each component from the filesystem root down to `path.parent` is opened
    with `O_NOFOLLOW` via the previous component's descriptor, so a
    symlinked ancestor is rejected instead of silently followed the way
    `Path.mkdir()`/string-path `os.open()` would follow it. Missing
    directories are created with `os.mkdir(..., dir_fd=...)` relative to
    the already-validated descriptor rather than through `path.parent`.
    """
    # `_descend_validated_dir_fd` always closes the descriptor it is handed
    # -- on success once the child is open, on failure via its own
    # `finally` -- so this loop must never also close `descriptor` itself;
    # doing so would close an already-closed (or, worse, reused) fd number.
    anchored = path if path.is_absolute() else Path.cwd() / path
    parts = anchored.parent.parts
    descriptor = os.open(parts[0], _DIRECTORY_OPEN_FLAGS)
    for component in parts[1:]:
        descriptor = _descend_validated_dir_fd(descriptor, component)
    return descriptor


def _descend_validated_dir_fd(parent_descriptor: int, name: str) -> int:
    """Validate/create `name` under `parent_descriptor`, returning its own descriptor.

    `parent_descriptor` is closed once the child descriptor is open.
    """
    try:
        try:
            named_metadata = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
        except FileNotFoundError:
            try:
                os.mkdir(name, 0o700, dir_fd=parent_descriptor)
            except FileExistsError:
                pass
            named_metadata = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)

        if _is_reparse_point(named_metadata) or not stat.S_ISDIR(named_metadata.st_mode):
            raise OSError(
                f"durable MCP audit path must not contain a symlink or reparse point: {name}"
            )

        child_descriptor = os.open(name, _DIRECTORY_OPEN_FLAGS, dir_fd=parent_descriptor)
    finally:
        os.close(parent_descriptor)

    try:
        child_metadata = os.fstat(child_descriptor)
        if (
            not stat.S_ISDIR(child_metadata.st_mode)
            or _is_reparse_point(named_metadata)
            or _identity(child_metadata) != _identity(named_metadata)
        ):
            raise OSError(
                f"durable MCP audit path must not contain a symlink or reparse point: {name}"
            )
        return child_descriptor
    except BaseException:
        os.close(child_descriptor)
        raise


def _open_audit_file_dir_fd(path: Path) -> int:
    """Open an owner-only regular file without following the final directory entries."""

    directory_descriptor = _open_validated_parent_dir_fd(path)
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
