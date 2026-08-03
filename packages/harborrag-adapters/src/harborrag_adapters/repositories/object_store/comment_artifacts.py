from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from hashlib import sha256

from harborrag_adapters.repositories.object_store.ingestion_artifacts import (
    ARTIFACT_BUCKET,
    ImmutableArtifact,
    ImmutableArtifactReader,
    ImmutableArtifactWriter,
    IngestionArtifactLayout,
)
from harborrag_core.domain import Document, DocumentElement
from harborrag_core.ingestion import (
    ArtifactReference,
    CanonicalComment,
    CanonicalCommentSet,
    reject_runtime_fields,
)
from harborrag_core.schemas.ids import DocumentId, DocumentVersionId
from harborrag_core.storage import StorageOperationContext


class CanonicalCommentSetBuilder:
    """Normalize provider comment metadata into deterministic content units."""

    def build(
        self,
        document: Document,
        *,
        document_version_id: str,
    ) -> CanonicalCommentSet:
        values: dict[str, dict[str, str | None]] = {}
        for item in self._mapping_items(document.provenance.extra.get("comments")):
            comment_id = self._text(item.get("comment_id") or item.get("id"))
            body = self._text(item.get("body") or item.get("text"))
            if comment_id is None or body is None:
                continue
            values[comment_id] = self._comment_values(
                comment_id=comment_id,
                body=body,
                metadata=item,
            )
        for element in document.content:
            if not self._is_comment(element):
                continue
            comment_id = self._text(
                element.metadata.get("comment_id") or element.metadata.get("id") or element.id
            )
            body = self._text(element.content)
            if comment_id is None or body is None:
                continue
            existing = values.get(comment_id, {})
            normalized = self._comment_values(
                comment_id=comment_id,
                body=body,
                metadata=element.metadata,
            )
            values[comment_id] = {
                key: value if value is not None else existing.get(key)
                for key, value in normalized.items()
            }
        comments = tuple(self._comment(values[comment_id]) for comment_id in sorted(values))
        return CanonicalCommentSet(
            document_id=DocumentId(document.id),
            document_version_id=DocumentVersionId(document_version_id),
            comments=comments,
        )

    @staticmethod
    def _is_comment(element: DocumentElement) -> bool:
        role = str(element.metadata.get("role") or "").casefold()
        return (
            element.metadata.get("comment_id") is not None
            or role == "comment"
            or role.endswith(".comment")
        )

    @classmethod
    def _comment_values(
        cls,
        *,
        comment_id: str,
        body: str,
        metadata: Mapping[str, object],
    ) -> dict[str, str | None]:
        return {
            "comment_id": comment_id,
            "comment_kind": cls._text(metadata.get("comment_kind")) or "COMMENT",
            "body": body,
            "author": cls._text(metadata.get("author")),
            "created_at": cls._text(metadata.get("created_at") or metadata.get("created")),
            "updated_at": cls._text(metadata.get("updated_at") or metadata.get("updated")),
            "parent_comment_id": cls._text(metadata.get("parent_comment_id")),
            "status": cls._text(metadata.get("status")),
        }

    @staticmethod
    def _comment(values: Mapping[str, str | None]) -> CanonicalComment:
        version_payload = json.dumps(
            values,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return CanonicalComment(
            comment_id=values["comment_id"] or "",
            comment_version=sha256(version_payload).hexdigest(),
            comment_kind=values["comment_kind"] or "",
            body=values["body"] or "",
            author=values["author"],
            created_at=values["created_at"],
            updated_at=values["updated_at"],
            parent_comment_id=values["parent_comment_id"],
            status=values["status"],
        )

    @staticmethod
    def _mapping_items(
        value: object,
    ) -> tuple[Mapping[str, object], ...]:
        if not isinstance(value, Sequence) or isinstance(
            value,
            (str, bytes, bytearray),
        ):
            return ()
        return tuple(item for item in value if isinstance(item, Mapping))

    @staticmethod
    def _text(value: object) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        return text or None


class CanonicalCommentArtifactRepository:
    """Persist and reload one immutable typed comment set per document version."""

    def __init__(
        self,
        writer: ImmutableArtifactWriter,
        reader: ImmutableArtifactReader,
        builder: CanonicalCommentSetBuilder | None = None,
    ) -> None:
        self._writer = writer
        self._reader = reader
        self._builder = builder or CanonicalCommentSetBuilder()

    async def put(
        self,
        document: Document,
        *,
        document_version_id: str,
        context: StorageOperationContext,
    ) -> tuple[CanonicalCommentSet, ArtifactReference]:
        comments = self._builder.build(
            document,
            document_version_id=document_version_id,
        )
        value = comments.model_dump(mode="json")
        reject_runtime_fields(value)
        payload = json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        reference = await self._writer.put(
            ImmutableArtifact(
                bucket=ARTIFACT_BUCKET,
                key=IngestionArtifactLayout.comments(
                    str(comments.document_id),
                    str(comments.document_version_id),
                ),
                payload=payload,
                media_type="application/json",
                artifact_kind="canonical-comments",
            ),
            context=context,
        )
        return comments, reference

    async def get(
        self,
        reference: ArtifactReference,
        *,
        context: StorageOperationContext,
    ) -> CanonicalCommentSet:
        value = json.loads(await self._reader.get(reference, context=context))
        reject_runtime_fields(value)
        return CanonicalCommentSet.model_validate(value)
