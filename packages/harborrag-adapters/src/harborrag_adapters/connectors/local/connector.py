"""Local filesystem discovery and raw-file loading orchestration."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path

from harborrag_core.domain.raw_document import RawDocument
from harborrag_core.domain.source import SourceRecord

from harborrag_adapters.connectors.base import BaseConnector
from harborrag_adapters.connectors.exceptions import (
    DocumentProcessingError,
    FetchError,
)
from harborrag_adapters.connectors.schemas import ConnectorCapabilities, ConnectorQuery

from .config import LocalFileConfig
from .filesystem import LocalFileSystem
from .mappers import build_document_metadata, path_from_record
from .utils import guess_mime_type, sha256_file

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
        for path in self._files.files_from_query(query):
            yield self._files.source_record(path)
            yielded += 1
            if query.limit is not None and yielded >= query.limit:
                return

    def load(self, record: SourceRecord) -> RawDocument:
        """Read one local file as bytes and attach filesystem metadata."""
        path = self._files.resolve_candidate(path_from_record(record))
        if not path.is_file():
            raise DocumentProcessingError(f"Local path is not a file: {path}")

        logger.info("Loading local file %s", path)
        try:
            stat = path.stat()
            self._files.enforce_size_limit(path, stat.st_size)
            content = path.read_bytes()
        except OSError as exc:
            raise FetchError(f"Could not read local file {path}: {exc}") from exc

        metadata = build_document_metadata(
            path,
            root_path=self.root_path,
            checksum=sha256_file(path),
        )

        return RawDocument(
            id=record.id,
            source=path.as_uri(),
            content=content,
            content_type=guess_mime_type(path),
            metadata=metadata.to_dict(),
            raw={"path": str(path)},
        )

    def load_by_paths(self, paths: list[str | Path]) -> Iterator[RawDocument]:
        """Load files for callers that already have file paths."""
        for path in paths:
            yield self.load(self._files.record_for_path(path))
