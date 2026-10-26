"""Safe reader-facing source catalog contracts."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field

from harborrag_core.base import StrictModel
from harborrag_core.security import AccessContext


class SourceEntityFacet(StrictModel):
    """One filterable fact every entity summary in a scope carries.

    Listed with the source because ``find_entities`` takes facets by name and a
    caller had no other way to learn them: the vocabulary is closed per scope,
    and a facet name the scope never declared silently matches nothing.
    """

    name: str = Field(min_length=1, max_length=64)
    type: Literal["text", "integer"] = "text"
    # The connector field the value is copied from (a Jira custom field's display
    # name, or an issue attribute such as ``status``), so a caller can map
    # ``skill_set`` back to the field a user knows as *Skill Set*.
    field: str = Field(min_length=1, max_length=256)


type EntitySummaryState = Literal["disabled", "idle", "queued", "running", "blocked", "failed"]


class ReadableSource(StrictModel):
    """One corpus scope a principal may read, without connection configuration."""

    source_scope_id: str = Field(min_length=1, max_length=128)
    connector_type: str = Field(min_length=1, max_length=64)
    display_name: str = Field(min_length=1, max_length=256)
    ingestion_state: str | None = Field(default=None, max_length=32)
    last_source_check_at: datetime | None = None
    last_successful_source_check_at: datetime | None = None
    last_successful_ingestion_at: datetime | None = None
    active_document_count: int = Field(default=0, ge=0)
    entity_facets: tuple[SourceEntityFacet, ...] = Field(default=(), max_length=12)
    # ``disabled`` when no summary policy covers the scope -- its entities have no
    # cards, so ``find_entities`` cannot reach them -- else the summary run state.
    entity_summaries: EntitySummaryState | None = None


class SourceCatalogQuery(StrictModel):
    """Bounded source page selector passed through the runtime port."""

    tenant_id: str = Field(min_length=1, max_length=128)
    access: AccessContext
    source_scope_ids: tuple[str, ...] = Field(default=(), max_length=20)
    connector_types: tuple[str, ...] = Field(default=(), max_length=10)
    after_source_scope_id: str | None = Field(default=None, max_length=128)
    limit: int = Field(default=20, ge=1, le=21)


__all__ = ["EntitySummaryState", "ReadableSource", "SourceCatalogQuery", "SourceEntityFacet"]
