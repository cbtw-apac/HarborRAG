from __future__ import annotations

import math
from collections.abc import Sequence
from enum import StrEnum
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .headers import ModelHeaderValue, protect_model_headers
from .metadata import ModelRequestMetadata
from .usage import ModelTokenUsage

type TokenSequence = tuple[int, ...]
type EmbeddingInput = str | TokenSequence
type EmbeddingValue = tuple[float, ...] | str
type RawEmbeddingInput = str | Sequence[str] | Sequence[int] | Sequence[Sequence[int]]


class EmbeddingEncodingFormat(StrEnum):
    """Enumerate supported embedding encoding format values."""

    FLOAT = "float"
    BASE64 = "base64"


class EmbeddingPurpose(StrEnum):
    """Enumerate supported embedding purpose values."""

    UNSPECIFIED = "unspecified"
    QUERY = "query"
    DOCUMENT = "document"
    CLASSIFICATION = "classification"
    CLUSTERING = "clustering"


class HarborEmbedMetadata(ModelRequestMetadata):
    """Carry provider-neutral embedding request metadata."""

    document_ids: tuple[str, ...] = ()
    chunk_ids: tuple[str, ...] = ()
    embedding_purpose: EmbeddingPurpose | None = None


class HarborEmbedRequest(BaseModel):
    """Represent a validated, provider-neutral embedding request."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    inputs: tuple[EmbeddingInput, ...]
    logical_model: str | None = None
    dimensions: int | None = Field(default=None, gt=0)
    encoding_format: EmbeddingEncodingFormat | None = None
    purpose: EmbeddingPurpose | None = None
    normalize: bool | None = None
    user: str | None = None
    batch_size: int | None = Field(default=None, gt=0)
    metadata: HarborEmbedMetadata = Field(default_factory=HarborEmbedMetadata)
    custom_headers: dict[str, ModelHeaderValue] = Field(default_factory=dict)
    extra_params: dict[str, Any] = Field(default_factory=dict)
    cacheable: bool = True
    sensitive: bool = False

    @field_validator("custom_headers", mode="before")
    @classmethod
    def protect_headers(cls, value: Any) -> Any:
        """Wrap credential-bearing header values before validation."""
        return protect_model_headers(value)

    @field_validator("inputs")
    @classmethod
    def validate_inputs(cls, value: tuple[EmbeddingInput, ...]) -> tuple[EmbeddingInput, ...]:
        """Reject empty inputs, empty token arrays, and negative token IDs."""
        if not value:
            raise ValueError("embedding request requires at least one input")
        for item in value:
            if isinstance(item, str):
                if not item.strip():
                    raise ValueError("embedding inputs cannot be empty strings")
            elif not item:
                raise ValueError("token-array inputs cannot be empty")
            elif any(token < 0 for token in item):
                raise ValueError("token IDs must be non-negative")
        return value

    @model_validator(mode="after")
    def validate_format(self) -> Self:
        """Reject normalization together with base64 encoding."""
        if self.normalize is True and self.encoding_format is EmbeddingEncodingFormat.BASE64:
            raise ValueError("normalize=true requires encoding_format=float")
        return self


class HarborEmbedding(BaseModel):
    """Store one normalized embedding vector and its source input index."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    index: int = Field(ge=0)
    value: EmbeddingValue
    dimensions: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def validate_dimensions(self) -> Self:
        """Ensure declared dimensions and vector finiteness are consistent."""
        if isinstance(self.value, tuple):
            if self.dimensions is not None and self.dimensions != len(self.value):
                raise ValueError("embedding dimensions do not match vector length")
            if any(not math.isfinite(value) for value in self.value):
                raise ValueError("embedding vectors must contain finite values")
        return self


class HarborEmbedUsage(ModelTokenUsage):
    """Represent normalized embedding usage returned by a model client."""

    prompt_tokens: int = Field(default=0, ge=0)

    def __add__(self, other: HarborEmbedUsage) -> HarborEmbedUsage:
        """Sum two usage totals."""
        return HarborEmbedUsage(
            prompt_tokens=self.prompt_tokens + other.prompt_tokens,
            total_tokens=self.total_tokens + other.total_tokens,
        )


class HarborEmbedResponse(BaseModel):
    """Represent a normalized, provider-neutral embedding response."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    embeddings: tuple[HarborEmbedding, ...]
    logical_model: str
    embedding_space: str
    provider: str
    provider_model: str
    deployment: str
    request_id: str
    usage: HarborEmbedUsage = Field(default_factory=HarborEmbedUsage)
    estimated_cost_usd: float | None = Field(default=None, ge=0)
    latency_ms: float | None = Field(default=None, ge=0)
    retry_count: int = Field(default=0, ge=0)
    fallback_count: int = Field(default=0, ge=0)
    cache_hit: bool = False
    normalized: bool = False
    provider_request_id: str | None = None
    provider_metadata: dict[str, Any] = Field(default_factory=dict)

    @property
    def vectors(self) -> tuple[EmbeddingValue, ...]:
        """Return each embedding value in input order."""
        return tuple(item.value for item in self.embeddings)

    @property
    def dimensions(self) -> int | None:
        """Return shared dimensions, or ``None`` when they are ambiguous."""
        dimensions = {item.dimensions for item in self.embeddings if item.dimensions is not None}
        return next(iter(dimensions)) if len(dimensions) == 1 else None
