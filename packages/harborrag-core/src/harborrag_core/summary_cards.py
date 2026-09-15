"""Bounded summary presentation values with no ingestion/storage dependencies."""

import hashlib
import json
from datetime import datetime
from typing import Literal, Self

from pydantic import Field, model_validator

from harborrag_core.base import StrictModel


class SummaryCard(StrictModel):
    description: str = Field(min_length=1, max_length=480)
    topics: tuple[str, ...] = Field(default=(), max_length=12)
    key_entities: tuple[str, ...] = Field(default=(), max_length=12)
    content_types: tuple[str, ...] = Field(default=(), max_length=8)

    @model_validator(mode="after")
    def bounded(self) -> Self:
        if not self.description.strip() or len(self.description.split()) > 60:
            raise ValueError("description requires 1 to 60 words")
        for value in (*self.topics, *self.key_entities, *self.content_types):
            if not value.strip() or len(value) > 80:
                raise ValueError("card facets must contain 1 to 80 characters")
        return self

    @property
    def artifact_hash(self) -> str:
        return hashlib.sha256(
            json.dumps(
                self.model_dump(mode="json"),
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode()
        ).hexdigest()


class SummaryView(StrictModel):
    status: Literal["none", "pending", "current", "stale"] = "none"
    execution: Literal["idle", "queued", "running", "blocked", "failed"] = "idle"
    card: SummaryCard | None = None
    coverage_mode: Literal["complete", "empty"] | None = None
    included_chunks: int | None = None
    child_count: int | None = None
    artifact_hash: str | None = None
    revision: int | None = None
    updated_at: datetime | None = None
    error_code: str | None = None
