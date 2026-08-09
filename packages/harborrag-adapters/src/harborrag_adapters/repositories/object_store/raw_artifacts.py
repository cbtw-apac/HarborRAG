from __future__ import annotations

import json
from hashlib import sha256

from harborrag_adapters.repositories.object_store.ingestion_artifacts import (
    RAW_BUCKET,
    ImmutableArtifact,
    ImmutableArtifactReader,
    ImmutableArtifactWriter,
    IngestionArtifactLayout,
)
from harborrag_core.chunking import ConnectorType
from harborrag_core.contracts import HarborConflictError
from harborrag_core.domain.raw_document import RawDocument
from harborrag_core.ingestion import RawDocumentReference, reject_runtime_fields
from harborrag_core.schemas.ids import DocumentId
from harborrag_core.storage import StorageOperationContext


class RawDocumentArtifactRepository:
    """Capture original bytes and replay-safe connector metadata immutably."""

    def __init__(
        self,
        writer: ImmutableArtifactWriter,
        reader: ImmutableArtifactReader,
    ) -> None:
        self._writer = writer
        self._reader = reader

    async def put(
        self,
        *,
        connector: ConnectorType,
        document_id: DocumentId,
        document: RawDocument,
        context: StorageOperationContext,
    ) -> RawDocumentReference:
        payload = (
            document.content.encode("utf-8")
            if isinstance(document.content, str)
            else document.content
        )
        content_hash = sha256(payload).hexdigest()
        source = await self._writer.put(
            ImmutableArtifact(
                bucket=RAW_BUCKET,
                key=IngestionArtifactLayout.raw(
                    connector,
                    document_id,
                    content_hash,
                ),
                payload=payload,
                media_type=document.content_type,
                artifact_kind="raw-source",
            ),
            context=context,
        )
        metadata_payload = self._metadata_payload(document)
        metadata_hash = sha256(metadata_payload).hexdigest()
        metadata_reference = await self._writer.put(
            ImmutableArtifact(
                bucket=RAW_BUCKET,
                key=IngestionArtifactLayout.raw_metadata(
                    connector,
                    document_id,
                    content_hash,
                    metadata_hash,
                ),
                payload=metadata_payload,
                media_type="application/json",
                artifact_kind="raw-metadata",
            ),
            context=context,
        )
        return RawDocumentReference(
            document_id=document_id,
            connector_type=connector.value,
            content_hash=content_hash,
            source_artifact=source,
            metadata_artifact=metadata_reference,
        )

    async def get(
        self,
        reference: RawDocumentReference,
        *,
        context: StorageOperationContext,
    ) -> RawDocument:
        metadata = json.loads(await self._reader.get(reference.metadata_artifact, context=context))
        reject_runtime_fields(metadata)
        content = await self._reader.get(reference.source_artifact, context=context)
        if sha256(content).hexdigest() != reference.content_hash:
            raise HarborConflictError("raw artifact checksum does not match its reference")
        source_payload = metadata.get("source_payload")
        if source_payload is not None and not isinstance(source_payload, dict):
            raise HarborConflictError("raw source payload is not a JSON object")
        return RawDocument(
            id=metadata["id"],
            source=metadata["source"],
            content=content,
            content_type=metadata["content_type"],
            metadata=dict(metadata.get("metadata") or {}),
            raw=source_payload,
        )

    @staticmethod
    def _metadata_payload(document: RawDocument) -> bytes:
        metadata = {
            "id": document.id,
            "source": document.source,
            "content_type": document.content_type,
            "metadata": document.metadata,
        }
        reject_runtime_fields(metadata)
        if document.raw is not None:
            metadata["source_payload"] = document.raw
        return json.dumps(
            metadata,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
