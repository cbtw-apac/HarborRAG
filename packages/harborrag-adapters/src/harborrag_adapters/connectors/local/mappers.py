"""Mappings from local files to source records and metadata."""

from __future__ import annotations

import os
from pathlib import Path

from harborrag_core.domain.source import SourceRecord

from .filesystem_paths import (
    guess_mime_type,
    relative_path,
    stat_datetime,
)
from .schemas import LocalFileMetadata


def path_from_record(record: SourceRecord) -> Path:
    """Return the path from a record's metadata or locator.

    Raises:
        ValueError: If the record has no path.
    """
    path = record.metadata.get("path") or record.locator
    if not path:
        raise ValueError(f"SourceRecord {record.id!r} does not contain path")
    return Path(str(path))


def build_source_record(
    path: Path,
    *,
    root_path: Path,
    checksum: str | None,
    is_symlink: bool,
) -> SourceRecord:
    """Build the source record for a local file.

    Args:
        path: Resolved file path.
        root_path: Trusted root; the record id is the path relative to it.
        checksum: Change-detection signature, or ``None``.
        is_symlink: Whether the path was a symlink before resolution.
    """
    stat = path.stat()
    relative = relative_path(path, root_path)
    mime_type = guess_mime_type(path)

    return SourceRecord(
        id=relative,
        source_type=mime_type,
        locator=relative,
        updated_at=stat_datetime(stat.st_mtime),
        checksum=checksum,
        metadata={
            "source_system": "local",
            "relative_path": relative,
            "name": path.name,
            "parent_relative_path": Path(relative).parent.as_posix(),
            "suffix": path.suffix.lower(),
            "size": stat.st_size,
            "created_at": stat_datetime(stat.st_ctime),
            "accessed_at": stat_datetime(stat.st_atime),
            "is_symlink": is_symlink,
        },
    )


def build_document_metadata(
    path: Path,
    *,
    root_path: Path,
    checksum: str,
    is_symlink: bool,
    stat_result: os.stat_result | None = None,
) -> LocalFileMetadata:
    """Build the metadata for a loaded local file.

    Args:
        path: Resolved file path.
        root_path: Trusted root for the relative path.
        checksum: Checksum of the loaded bytes.
        is_symlink: Whether the path was a symlink before resolution.
        stat_result: ``stat`` taken from the read descriptor; read from ``path`` if ``None``.
    """
    stat = stat_result or path.stat()
    relative = relative_path(path, root_path)
    return LocalFileMetadata(
        record_id=relative,
        title=path.name,
        checksum=checksum,
        created_at=stat_datetime(stat.st_ctime),
        updated_at=stat_datetime(stat.st_mtime),
        relative_path=relative,
        name=path.name,
        parent_relative_path=Path(relative).parent.as_posix(),
        suffix=path.suffix.lower(),
        size=stat.st_size,
        accessed_at=stat_datetime(stat.st_atime),
        is_symlink=is_symlink,
    )
