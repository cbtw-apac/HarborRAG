"""Secret-free source query and processing inputs shared by execution providers."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime

from harborrag_core.security.field_names import canonical_field_name, is_sensitive_field_name

# A presigned URL is a credential-bearing *value*; core's definition is about names
# that claim to hold a credential, so this one stays local to source queries.
_SIGNED_URL_FIELDS = frozenset({"signed_url"})


@dataclass(frozen=True, slots=True)
class ProcessingProfileInput:
    parser_profile: str
    normalizer_version: str
    chunk_strategy: str
    dense_encoder_profile: str
    sparse_encoder_profile: str
    graph_projection_version: str
    vector_projection_schema: str = "vector-payload-v1"

    def __post_init__(self) -> None:
        if any(
            not value.strip()
            for value in (
                self.parser_profile,
                self.normalizer_version,
                self.chunk_strategy,
                self.dense_encoder_profile,
                self.sparse_encoder_profile,
                self.graph_projection_version,
                self.vector_projection_schema,
            )
        ):
            raise ValueError("processing profile values must be non-empty")


@dataclass(frozen=True, slots=True)
class SourceQuery:
    path: str | None = None
    pattern: str | None = None
    recursive: bool = True
    updated_after: str | None = None
    limit: int | None = None
    include_attachments: bool = True
    filters_json: str = "{}"

    def __post_init__(self) -> None:
        filters = json.loads(self.filters_json)
        if not isinstance(filters, dict):
            raise ValueError("source query filters must encode an object")
        _reject_secret_query_fields(filters)
        if self.limit is not None and self.limit < 1:
            raise ValueError("source query limit must be positive")
        if self.updated_after is not None:
            try:
                updated_after = datetime.fromisoformat(self.updated_after)
            except ValueError as error:
                raise ValueError("updated_after must be an ISO-8601 timestamp") from error
            if updated_after.tzinfo is None:
                raise ValueError("updated_after must include a timezone")


def _reject_secret_query_fields(value: object) -> None:
    if isinstance(value, Mapping):
        for raw_key, item in value.items():
            if is_sensitive_field_name(raw_key) or (
                canonical_field_name(raw_key) in _SIGNED_URL_FIELDS
            ):
                raise ValueError("source query filters cannot contain credentials")
            _reject_secret_query_fields(item)
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            _reject_secret_query_fields(item)
