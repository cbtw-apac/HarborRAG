from __future__ import annotations

from pydantic import Field, model_validator

from harborrag_core.base import StrictModel
from harborrag_core.schemas.ids import DocumentId, DocumentVersionId


class CanonicalComment(StrictModel):
    """One source comment retained as a versioned canonical content unit."""

    comment_id: str = Field(min_length=1)
    comment_version: str = Field(
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-f]{64}$",
    )
    comment_kind: str = Field(min_length=1)
    body: str = Field(min_length=1)
    author: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    parent_comment_id: str | None = None
    status: str | None = None


class CanonicalCommentSet(StrictModel):
    """Immutable comments artifact for one canonical document version."""

    schema_version: int = Field(default=1, ge=1)
    document_id: DocumentId
    document_version_id: DocumentVersionId
    comments: tuple[CanonicalComment, ...] = ()

    @model_validator(mode="after")
    def validate_comment_identities(self) -> CanonicalCommentSet:
        comment_ids = tuple(comment.comment_id for comment in self.comments)
        if len(set(comment_ids)) != len(comment_ids):
            raise ValueError("canonical comment IDs must be unique")
        if tuple(sorted(comment_ids)) != comment_ids:
            raise ValueError("canonical comments must be ordered by comment ID")
        return self
