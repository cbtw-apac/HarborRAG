"""Descriptor-bound local file opening beneath a trusted root."""

from __future__ import annotations

import os
import stat as stat_module
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from harborrag_adapters.connectors.exceptions import DocumentProcessingError, FetchError


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


def read_snapshot_beneath(
    path: Path,
    *,
    scope: SecureReadScope,
    enforce_size_limit: Callable[[Path, int], None],
    read_descriptor: Callable[[Path, int, os.stat_result], LocalFileSnapshot],
) -> LocalFileSnapshot:
    """Open every component without following links and read the bound inode."""

    if scope.root_fd < 0:
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

    directory_fd = os.dup(scope.root_fd)
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
