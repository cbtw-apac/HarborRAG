"""Local filesystem discovery and raw-file loading orchestration."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path

from harborrag_adapters.connectors.base import BaseConnector
from harborrag_adapters.connectors.exceptions import (
    DocumentProcessingError,
    FetchError,
)
from harborrag_adapters.connectors.schemas import ConnectorCapabilities, ConnectorQuery
from harborrag_core.domain.raw_document import RawDocument
from harborrag_core.domain.source import SourceRecord

from .config import LocalFileConfig
from .filesystem import LocalFileSystem
from .filesystem_paths import guess_mime_type
from .mappers import build_document_metadata, path_from_record

logger = logging.getLogger("harborrag.adapters.connectors.local")


class LocalFileConnector(BaseConnector):
    """Connector for local files and folders.

    The configured source path is the trust boundary. Discovery and direct loads
    resolve candidate paths and reject anything outside that scope, including
    symlink escapes.
    """

    provider_name = "local"
    capabilities = ConnectorCapabilities(
        pagination=False,
        incremental_sync=True,
        full_sync=True,
        relationships=True,
        local_files=True,
    )

    def __init__(self, config: LocalFileConfig) -> None:
        """Initialize safe filesystem operations for the configured scope."""
        self.config = config
        self._files = LocalFileSystem(config)
        self.source_path = self._files.source_path
        self.root_path = self._files.root_path

    def discover(self, query: ConnectorQuery | None = None) -> Iterator[SourceRecord]:
        """Discover files under the configured source path or explicit paths."""
        query = query or ConnectorQuery()
        yielded = 0
        for path, is_symlink in self._files.files_from_query(query):
            yield self._files.source_record(path, is_symlink=is_symlink)
            yielded += 1
            if query.limit is not None and yielded >= query.limit:
                return

    def load(self, record: SourceRecord) -> RawDocument:
        """Read one local file as bytes and attach filesystem metadata."""
        record_path = path_from_record(record)
        path = record_path if record_path.is_absolute() else self.root_path / record_path
        if not path.is_relative_to(self.root_path):
            raise DocumentProcessingError(f"Local path is outside configured source scope: {path}")

        logger.info("Loading local file %s", path)
        try:
            snapshot = self._files.read_snapshot(path)
        except OSError as exc:
            raise FetchError(f"Could not read local file {path}") from exc

        metadata = build_document_metadata(
            path,
            root_path=self.root_path,
            checksum=snapshot.checksum,
            is_symlink=bool(record.metadata.get("is_symlink", False)),
            stat_result=snapshot.stat,
        )

        return RawDocument(
            id=record.id,
            source=path.as_uri(),
            content=snapshot.content,
            content_type=guess_mime_type(path),
            metadata=metadata.to_dict(),
            raw={"path": str(path)},
        )

    def close(self) -> None:
        """Release the root descriptor held by the filesystem boundary."""

        self._files.close()

    def load_by_paths(self, paths: list[str | Path]) -> Iterator[RawDocument]:
        """Load files for callers that already have file paths."""
        for path in paths:
            yield self.load(self._files.record_for_path(path))
