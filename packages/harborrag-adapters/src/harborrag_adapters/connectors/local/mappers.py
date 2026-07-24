"""Mappings from local files to Harbor domain objects."""

from __future__ import annotations

from pathlib import Path

from harborrag_core.domain.source import SourceRecord

from .filesystem_paths import (
    guess_mime_type,
    relative_path,
    stat_datetime,
)
from .schemas import LocalFileMetadata


def path_from_record(record: SourceRecord) -> Path:
    """Recover the filesystem path from a local source record."""
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
    """Convert a local file path into a lightweight source record.

    ``is_symlink`` must be captured by the caller before ``path`` is resolved —
    traversal resolves symlinks to their real target, so ``path.is_symlink()``
    here would always be ``False``.
    """
    stat = path.stat()
    relative = relative_path(path, root_path)
    mime_type = guess_mime_type(path)

    return SourceRecord(
        id=path.as_uri(),
        source_type=mime_type,
        locator=str(path),
        updated_at=stat_datetime(stat.st_mtime),
        checksum=checksum,
        metadata={
            "source_system": "local",
            "path": str(path),
            "relative_path": relative,
            "name": path.name,
            "parent_path": str(path.parent),
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
) -> LocalFileMetadata:
    """Build parsed provenance metadata for a loaded local file.

    ``is_symlink`` must be captured by the caller before ``path`` is resolved;
    see :func:`build_source_record`.
    """
    stat = path.stat()
    relative = relative_path(path, root_path)
    return LocalFileMetadata(
        record_id=relative,
        title=path.name,
        checksum=checksum,
        created_at=stat_datetime(stat.st_ctime),
        updated_at=stat_datetime(stat.st_mtime),
        path=str(path),
        relative_path=relative,
        name=path.name,
        parent_path=str(path.parent),
        suffix=path.suffix.lower(),
        size=stat.st_size,
        accessed_at=stat_datetime(stat.st_atime),
        is_symlink=is_symlink,
    )
