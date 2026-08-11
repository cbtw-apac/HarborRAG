"""Descriptor-bound local file opening beneath a trusted root."""

from __future__ import annotations

import os
import stat as stat_module
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from harborrag_adapters.connectors.exceptions import DocumentProcessingError, FetchError

from .filesystem_paths import SUPPORTS_DIR_FD


@dataclass(frozen=True, slots=True)
class LocalFileSnapshot:
    """Bytes and metadata captured from one already-authorized file descriptor."""

    content: bytes
    stat: os.stat_result
    checksum: str


@dataclass(frozen=True, slots=True)
class SecureReadScope:
    """Trusted descriptor and path scope for one local connector."""

    root_path: Path
    source_path: Path
    source_is_file: bool
    root_fd: int
    closed: bool = False


def read_snapshot_beneath(
    path: Path,
    *,
    scope: SecureReadScope,
    enforce_size_limit: Callable[[Path, int], None],
    read_descriptor: Callable[[Path, int, os.stat_result], LocalFileSnapshot],
) -> LocalFileSnapshot:
    """Validate scope, then hand off to the platform-appropriate open strategy."""

    if scope.closed:
        raise RuntimeError("local filesystem connector is closed")
    try:
        relative = path.relative_to(scope.root_path)
    except ValueError as exc:
        raise DocumentProcessingError(
            f"Local path is outside configured source scope: {path}"
        ) from exc
    if scope.source_is_file and relative != scope.source_path.relative_to(scope.root_path):
        raise DocumentProcessingError(f"Local path is outside configured source scope: {path}")
    if not relative.parts:
        raise DocumentProcessingError(f"Local path is not a file: {path}")
    if any(part in {"", ".", ".."} for part in relative.parts):
        raise DocumentProcessingError(f"Invalid local file path: {path}")

    if SUPPORTS_DIR_FD:
        return _read_via_dir_fd(
            path,
            relative,
            root_fd=scope.root_fd,
            enforce_size_limit=enforce_size_limit,
            read_descriptor=read_descriptor,
        )
    return _read_via_plain_open(
        path,
        relative,
        root_path=scope.root_path,
        enforce_size_limit=enforce_size_limit,
        read_descriptor=read_descriptor,
    )


def _read_via_dir_fd(
    path: Path,
    relative: Path,
    *,
    root_fd: int,
    enforce_size_limit: Callable[[Path, int], None],
    read_descriptor: Callable[[Path, int, os.stat_result], LocalFileSnapshot],
) -> LocalFileSnapshot:
    """Open every component without following links and read the bound inode.

    Requires `dir_fd`/`openat()` support (POSIX only, see `SUPPORTS_DIR_FD`).
    """

    directory_fd = os.dup(root_fd)
    file_fd = -1
    try:
        directory_flags = (
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        for component in relative.parts[:-1]:
            next_fd = os.open(component, directory_flags, dir_fd=directory_fd)
            os.close(directory_fd)
            directory_fd = next_fd
        file_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        file_fd = os.open(relative.parts[-1], file_flags, dir_fd=directory_fd)
        descriptor_stat = os.fstat(file_fd)
        if not stat_module.S_ISREG(descriptor_stat.st_mode):
            raise DocumentProcessingError(f"Local path is not a regular file: {path}")
        enforce_size_limit(path, descriptor_stat.st_size)
        return read_descriptor(path, file_fd, descriptor_stat)
    except OSError as exc:
        raise FetchError(f"Could not securely open local file {path}") from exc
    finally:
        if file_fd >= 0:
            os.close(file_fd)
        os.close(directory_fd)


def _read_via_plain_open(
    path: Path,
    relative: Path,
    *,
    root_path: Path,
    enforce_size_limit: Callable[[Path, int], None],
    read_descriptor: Callable[[Path, int, os.stat_result], LocalFileSnapshot],
) -> LocalFileSnapshot:
    """Best-effort fallback for platforms without `dir_fd` support (Windows).

    Windows has no `openat()`/per-component `O_NOFOLLOW` equivalent, so this
    narrows rather than closes the TOCTOU window `_read_via_dir_fd` closes on
    POSIX: every intermediate component is checked for a symlink immediately
    before descending into it, then the final file is opened by plain path.
    `O_BINARY` is required here -- without it Windows' C runtime applies
    text-mode translation (CRLF rewriting, treating 0x1A as EOF) to reads
    through this descriptor, silently corrupting binary files such as PDFs.
    """

    current = root_path
    for component in relative.parts[:-1]:
        current = current / component
        if current.is_symlink():
            raise FetchError(
                f"Could not securely open local file {path}: "
                f"refusing to follow symlink at {current}"
            )
    final = current / relative.parts[-1]
    if final.is_symlink():
        raise FetchError(
            f"Could not securely open local file {path}: refusing to follow symlink at {final}"
        )

    file_fd = -1
    try:
        file_fd = os.open(final, os.O_RDONLY | getattr(os, "O_BINARY", 0))
        descriptor_stat = os.fstat(file_fd)
        if not stat_module.S_ISREG(descriptor_stat.st_mode):
            raise DocumentProcessingError(f"Local path is not a regular file: {path}")
        enforce_size_limit(path, descriptor_stat.st_size)
        return read_descriptor(path, file_fd, descriptor_stat)
    except OSError as exc:
        raise FetchError(f"Could not securely open local file {path}") from exc
    finally:
        if file_fd >= 0:
            os.close(file_fd)
