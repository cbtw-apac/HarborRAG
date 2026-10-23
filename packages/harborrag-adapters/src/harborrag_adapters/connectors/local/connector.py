"""Local filesystem connector.

Handles:
- Discovery under the source path or from explicit paths
- Descriptor-bound file loading beneath the trusted root
- Markdown and HTML link relations
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path

from harborrag_adapters.connectors.base import BaseConnector
from harborrag_adapters.connectors.exceptions import (
    DocumentProcessingError,
    FetchError,
)
from harborrag_adapters.connectors.schemas import (
    ConnectorCapabilities,
    ConnectorQuery,
    ConnectorSkip,
)
from harborrag_core.domain.raw_document import RawDocument
from harborrag_core.domain.source import SourceRecord

from .config import LocalFileConfig
from .filesystem import LocalFileSystem
from .filesystem_paths import guess_mime_type
from .mappers import build_document_metadata, path_from_record
from .relations import LocalDocumentRelationResolver

logger = logging.getLogger("harborrag.adapters.connectors.local")


class LocalFileConnector(BaseConnector):
    """Connector for local files and folders.

    Rejects paths outside the configured source path, including symlink escapes.
    ``close`` releases the root descriptor.
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
        """Open the filesystem boundary for the configured source path.

        Args:
            config: Validated local config.
        """
        self.config = config
        self._files = LocalFileSystem(config)
        self.source_path = self._files.source_path
        self.root_path = self._files.root_path
        self._relations = LocalDocumentRelationResolver(self.root_path)

    def discover(self, query: ConnectorQuery | None = None) -> Iterator[SourceRecord]:
        """Yield file records under the source path or from explicit paths.

        Explicit paths come from the ``file_paths``, ``paths`` or ``files`` filters. Files
        dropped for a reportable reason are listed in ``skipped``.

        Args:
            query: Path, pattern, filter and limit selectors.

        Yields:
            One ``SourceRecord`` per file that passes selection.

        Raises:
            DocumentProcessingError: If an explicit path is outside the source scope.
        """
        query = query or ConnectorQuery()
        yielded = 0
        for path, is_symlink in self._files.files_from_query(query):
            record = self._files.discovered_record(path, is_symlink=is_symlink)
            if record is None:
                continue
            yield record
            yielded += 1
            if query.limit is not None and yielded >= query.limit:
                return

    @property
    def skipped(self) -> tuple[ConnectorSkip, ...]:
        """Return the files traversal dropped, with reasons."""

        return self._files.skips.entries

    def load(self, record: SourceRecord) -> RawDocument:
        """Read one local file as a raw document.

        Args:
            record: File record produced by discovery.

        Returns:
            Raw document with the bytes, file metadata and ``links_to`` relations.

        Raises:
            DocumentProcessingError: If the path is outside the source scope, is not a
                regular file, or exceeds ``max_file_size_bytes``.
            FetchError: If the file cannot be read.
        """
        record_path = path_from_record(record)
        path = record_path if record_path.is_absolute() else self.root_path / record_path
        if not path.is_relative_to(self.root_path):
            raise DocumentProcessingError(f"Local path is outside configured source scope: {path}")

        logger.info("Loading local file %s", path)
        # TODO: apply the selection policy in ``load``, as ``record_for_path`` does.
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

        media_type = guess_mime_type(path)
        metadata_payload = metadata.to_dict()
        metadata_payload["relations"] = self._relations.relations(
            source_path=path,
            content=snapshot.content,
            media_type=media_type,
        )
        return RawDocument(
            id=record.id,
            source=f"local:///{metadata.relative_path}",
            content=snapshot.content,
            content_type=media_type,
            metadata=metadata_payload,
            raw={"relative_path": metadata.relative_path},
        )

    def close(self) -> None:
        """Close the root descriptor."""

        self._files.close()

    def load_by_paths(self, paths: list[str | Path]) -> Iterator[RawDocument]:
        """Yield raw documents for known file paths.

        Args:
            paths: Paths inside the source scope; each must pass the selection policy.
        """
        for path in paths:
            yield self.load(self._files.record_for_path(path))
