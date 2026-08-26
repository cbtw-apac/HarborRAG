from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Self

from pydantic import Field, field_validator, model_validator

from harborrag_core.base import StrictModel

from .headers import ProtectedModelHeaders
from .metadata import ModelRequestMetadata
from .usage import ModelTokenUsage

type RerankDocumentContent = str | dict[str, Any]
type RawRerankDocument = str | Mapping[str, Any] | HarborRerankDocument


class HarborRerankMetadata(ModelRequestMetadata):
    """Carry provider-neutral reranking request metadata."""

    retrieval_query: str | None = None
    reranker_stage: str | None = None
    retrieval_latency_ms: float | None = Field(default=None, ge=0)


class HarborRerankDocument(StrictModel):
    """Represent one text or structured document supplied to a reranker."""

    content: RerankDocumentContent
    document_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("content")
    @classmethod
    def validate_content(cls, value: RerankDocumentContent) -> RerankDocumentContent:
        """Reject empty text or structured document content."""
        if isinstance(value, str) and not value.strip():
            raise ValueError("rerank document text cannot be empty")
        if isinstance(value, dict) and not value:
            raise ValueError("structured rerank documents cannot be empty")
        return value

    @classmethod
    def text(
        cls,
        content: str,
        *,
        document_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Self:
        """Build a text document with optional identifier and metadata."""
        return cls(content=content, document_id=document_id, metadata=metadata or {})


class HarborRerankRequest(StrictModel):
    """Represent a validated, provider-neutral reranking request."""

    query: str = Field(min_length=1)
    documents: tuple[HarborRerankDocument, ...]
    logical_model: str | None = None
    top_n: int | None = Field(default=None, gt=0)
    rank_fields: tuple[str, ...] = ()
    return_documents: bool | None = None
    max_chunks_per_doc: int | None = Field(default=None, gt=0)
    max_tokens_per_doc: int | None = Field(default=None, gt=0)
    instruction: str | None = None
    metadata: HarborRerankMetadata = Field(default_factory=HarborRerankMetadata)
    custom_headers: ProtectedModelHeaders = Field(default_factory=dict)
    extra_params: dict[str, Any] = Field(default_factory=dict)
    cacheable: bool = True
    sensitive: bool = False

    @field_validator("query")
    @classmethod
    def validate_query(cls, value: str) -> str:
        """Reject a blank query string."""
        if not value.strip():
            raise ValueError("rerank query cannot be blank")
        return value

    @model_validator(mode="after")
    def validate_request(self) -> Self:
        """Validate documents, top_n bounds, and rank-field compatibility."""
        if not self.documents:
            raise ValueError("rerank request requires at least one document")
        if self.top_n is not None and self.top_n > len(self.documents):
            raise ValueError("top_n cannot exceed the number of documents")
        if self.rank_fields and any(
            isinstance(document.content, str) for document in self.documents
        ):
            raise ValueError("rank_fields requires every document to be structured")
        return self


class HarborRerankUsage(ModelTokenUsage):
    """Represent normalized reranking usage returned by a model client."""

    search_units: int = Field(default=0, ge=0)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)


class HarborRerankResult(StrictModel):
    """Represent one normalized reranking result."""

    rank: int = Field(ge=1)
    index: int = Field(ge=0)
    relevance_score: float
    document_id: str | None = None
    document: RerankDocumentContent | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class HarborRerankResponse(StrictModel):
    """Represent a normalized, provider-neutral reranking response."""

    results: tuple[HarborRerankResult, ...]
    logical_model: str
    provider: str
    provider_model: str
    deployment: str
    request_id: str
    response_id: str | None = None
    usage: HarborRerankUsage = Field(default_factory=HarborRerankUsage)
    estimated_cost_usd: float | None = Field(default=None, ge=0)
    latency_ms: float | None = Field(default=None, ge=0)
    retry_count: int = Field(default=0, ge=0)
    fallback_count: int = Field(default=0, ge=0)
    cache_hit: bool = False
    provider_request_id: str | None = None
    provider_metadata: dict[str, Any] = Field(default_factory=dict)

    @property
    def indices(self) -> tuple[int, ...]:
        """Return each result's source document index in rank order."""
        return tuple(result.index for result in self.results)
