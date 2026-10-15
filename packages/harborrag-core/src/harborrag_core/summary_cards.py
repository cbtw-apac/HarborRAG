"""Bounded summary presentation values with no ingestion/storage dependencies."""

import hashlib
import json
from collections.abc import Mapping
from datetime import datetime
from typing import Any, Literal, Self

from pydantic import Field, model_validator

from harborrag_core.base import StrictModel

# A navigation card and an entity dossier are the same product at two budgets: the
# ceiling here is the widest a card may ever be, and the per-kind budget that a
# generation actually has to satisfy lives in ``SummaryPolicy.card_words``. Keeping
# one type means every reader, cache and digest path stays identical whichever
# budget produced the card.
# 512 words at the 8-chars-per-word ratio; a comfortable fit under the 1024-token
# output budget the summarizer is given.
SUMMARY_DESCRIPTION_MAX_CHARS = 4096
SUMMARY_DESCRIPTION_MAX_WORDS = 512
SUMMARY_CARD_MAX_WORDS = 60
SUMMARY_CARD_MAX_CHARS = 480

SummaryCoverage = Literal["complete", "partial", "empty"]


class SummaryAttribute(StrictModel):
    """One named facet a card asserts, with the documents it was copied from.

    Attributes are what makes a card filterable rather than only readable: a
    retrieval payload can carry ``stage`` or ``skill_set`` without any of those
    names entering this package. Every value is copied verbatim from a structured
    field the connector already held -- the model never fills one -- and
    ``from_document_ids`` names the documents that field was read from.
    """

    name: str = Field(min_length=1, max_length=80)
    values: tuple[str, ...] = Field(min_length=1, max_length=24)
    from_document_ids: tuple[str, ...] = Field(default=(), max_length=64)

    @model_validator(mode="after")
    def bounded(self) -> Self:
        if not self.name.strip():
            raise ValueError("summary attribute name must be non-empty")
        for value in self.values:
            if not value.strip() or len(value) > 200:
                raise ValueError("summary attribute values contain 1 to 200 characters")
        if len(set(self.values)) != len(self.values):
            raise ValueError("summary attribute values must be distinct")
        return self


def card_digest(card: Mapping[str, Any]) -> str:
    """The integrity hash of a card's stored JSON form."""

    return hashlib.sha256(
        json.dumps(card, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()


def has_legacy_attributes(card: object) -> bool:
    """Whether a stored card predates source-field-only facets.

    Cards written while the model could also fill facets tagged every attribute
    with its ``source``; the model-read ones are no longer a thing a card asserts.
    """

    if not isinstance(card, Mapping):
        return False
    attributes = card.get("attributes")
    return isinstance(attributes, list | tuple) and any(
        isinstance(item, Mapping) and "source" in item for item in attributes
    )


class SummaryCard(StrictModel):
    description: str = Field(min_length=1, max_length=SUMMARY_DESCRIPTION_MAX_CHARS)
    topics: tuple[str, ...] = Field(default=(), max_length=12)
    key_entities: tuple[str, ...] = Field(default=(), max_length=12)
    content_types: tuple[str, ...] = Field(default=(), max_length=8)
    attributes: tuple[SummaryAttribute, ...] = Field(default=(), max_length=12)

    @model_validator(mode="before")
    @classmethod
    def upgrade_legacy_attributes(cls, value: Any) -> Any:
        """Read a card stored before facets became source-field only.

        Its source-field attributes are kept without the tag; the ones the model
        read out of content are dropped, since no current card may carry one.
        Callers that hold the stored hash verify it against the stored form first
        (see ``card_digest``), because the upgraded card hashes differently.
        """

        if not has_legacy_attributes(value):
            return value
        return {
            **value,
            "attributes": [
                {key: item for key, item in attribute.items() if key != "source"}
                for attribute in value["attributes"]
                if not (isinstance(attribute, Mapping) and attribute.get("source") == "extracted")
            ],
        }

    @model_validator(mode="after")
    def bounded(self) -> Self:
        if not self.description.strip() or (
            len(self.description.split()) > SUMMARY_DESCRIPTION_MAX_WORDS
        ):
            raise ValueError(f"description requires 1 to {SUMMARY_DESCRIPTION_MAX_WORDS} words")
        for value in (*self.topics, *self.key_entities, *self.content_types):
            if not value.strip() or len(value) > 80:
                raise ValueError("card facets must contain 1 to 80 characters")
        names = [attribute.name for attribute in self.attributes]
        if len(set(names)) != len(names):
            raise ValueError("card attribute names must be distinct")
        return self

    @property
    def artifact_hash(self) -> str:
        return card_digest(self.model_dump(mode="json"))


class SummaryView(StrictModel):
    status: Literal["none", "pending", "current", "stale"] = "none"
    execution: Literal["idle", "queued", "running", "blocked", "failed"] = "idle"
    card: SummaryCard | None = None
    coverage_mode: SummaryCoverage | None = None
    # Populated only when ``coverage_mode`` is ``partial``: the source items that
    # belong to this node and had no published document version to read.
    missing_documents: int | None = None
    included_chunks: int | None = None
    child_count: int | None = None
    artifact_hash: str | None = None
    revision: int | None = None
    updated_at: datetime | None = None
    error_code: str | None = None
