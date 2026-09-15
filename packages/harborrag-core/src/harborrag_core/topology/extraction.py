"""Bounded, evidence-addressed semantic extraction contracts."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Literal, Self

from pydantic import Field, model_validator

from harborrag_core.base import StrictModel

from .ontology import OntologyRegistry, builtin_ontology
from .text_policy import (
    CHUNK_DESCRIPTION_MAX_WORDS,
    CHUNK_TITLE_MAX_WORDS,
    ENTITY_DESCRIPTION_MAX_WORDS,
    RELATION_DESCRIPTION_MAX_WORDS,
    enforce_entity_name,
    enforce_text_budget,
)


class ExtractionIncompleteError(ValueError):
    """Input/output coverage is explicitly incomplete and cannot become ready."""


def digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()


class ExtractionProfile(StrictModel):
    model: str = Field(min_length=1, max_length=256)
    deployment_revision: str = Field(min_length=1, max_length=256)
    prompt_digest: str = Field(min_length=1, max_length=128)
    schema_version: str = "1"
    ontology_version: str = "1"
    context_policy: str = "chunk-v1"
    code_version: str = "1"
    max_input_chars: int = Field(default=12000, ge=100, le=100000)
    max_output_tokens: int = Field(default=2048, ge=128, le=16384)
    enable_thinking: bool | None = None
    reasoning_effort: Literal["minimal", "low", "medium", "high"] | None = None
    temperature: float | None = Field(default=None, ge=0, le=1)
    ontology: OntologyRegistry | None = None
    max_windows: int = Field(default=8, ge=1, le=64)

    def resolved_ontology(self) -> OntologyRegistry:
        registry = self.ontology or builtin_ontology()
        if self.schema_version != "1" and self.ontology_version != registry.version:
            raise ValueError("profile ontology version does not match frozen registry")
        return registry

    @property
    def fingerprint(self) -> str:
        payload = self.model_dump(mode="json")
        if self.schema_version == "1" and self.ontology is None and self.max_windows == 8:
            payload.pop("ontology")
            payload.pop("max_windows")
        return digest(payload)


class ChunkExtractionInput(StrictModel):
    chunk_id: str = Field(min_length=1, max_length=256)
    content: str = Field(max_length=100000)
    context: str = Field(default="", max_length=20000)
    source_title: str = Field(default="", max_length=1024)
    heading_path: tuple[str, ...] = Field(default=(), max_length=64)
    section_ids: tuple[str, ...] = Field(default=(), max_length=64)
    source_revision: str = Field(default="", max_length=256)
    processing_fingerprint: str = Field(default="", max_length=256)
    context_dependencies: tuple[str, ...] = Field(default=(), max_length=64)
    window_start: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def validate_section_identity(self) -> Self:
        if self.section_ids and len(self.section_ids) != len(self.heading_path):
            raise ValueError("section_ids must identify every heading_path component")
        if any(not value.strip() for value in self.section_ids):
            raise ValueError("section_ids must contain non-empty stable identities")
        return self

    @property
    def input_digest(self) -> str:
        payload = self.model_dump(mode="json", exclude={"chunk_id"}, exclude_defaults=True)
        payload.update(content=self.content, context=self.context)
        return digest(payload)


class EvidenceSpan(StrictModel):
    start: int = Field(ge=0)
    end: int = Field(gt=0)
    quote: str = Field(min_length=1, max_length=8000)
    source: Literal["content", "context", "source_title"] = "content"

    def validate_content(self, content: str) -> None:
        if (
            self.end <= self.start
            or self.end > len(content)
            or content[self.start : self.end] != self.quote
        ):
            raise ValueError("evidence span does not match immutable chunk content")


class ExtractedEntity(StrictModel):
    local_id: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=256)
    entity_type: str = Field(min_length=1, max_length=64)
    description: str = Field(default="", max_length=2000)
    description_evidence: tuple[EvidenceSpan, ...] = Field(default=(), max_length=32)
    span: EvidenceSpan
    aliases: tuple[str, ...] = Field(default=(), max_length=16)
    external_id: str | None = Field(default=None, max_length=512)


class ExtractedAssertion(StrictModel):
    local_id: str = Field(min_length=1, max_length=128)
    subject_id: str = Field(min_length=1, max_length=128)
    object_id: str = Field(min_length=1, max_length=128)
    predicate: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    span: EvidenceSpan
    polarity: Literal["affirmative", "negative"] = "affirmative"
    modality: Literal["asserted", "possible", "required"] = "asserted"
    time_qualifier: str | None = Field(default=None, max_length=256)
    statement_text: str = Field(default="", max_length=4000)
    attribution: str | None = Field(default=None, max_length=1000)
    qualifiers: tuple[str, ...] = Field(default=(), max_length=32)
    valid_from: str | None = Field(default=None, max_length=64)
    valid_to: str | None = Field(default=None, max_length=64)
    temporal_precision: Literal[
        "unknown", "year", "month", "day", "instant", "interval", "relative"
    ] = "unknown"

    @model_validator(mode="after")
    def validate_temporal_bounds(self) -> Self:
        formats = {"year": "%Y", "month": "%Y-%m", "day": "%Y-%m-%d"}
        bounds = [value for value in (self.valid_from, self.valid_to) if value is not None]
        if self.temporal_precision in formats:
            for value in bounds:
                parsed = datetime.strptime(value, formats[self.temporal_precision])
                if parsed.strftime(formats[self.temporal_precision]) != value:
                    raise ValueError("temporal bound does not match declared precision")
        elif self.temporal_precision == "instant":
            _validate_instants(bounds)
        if len(bounds) == 2 and self.temporal_precision in formats and bounds[0] > bounds[1]:
            raise ValueError("valid_from cannot follow valid_to")
        return self


def _validate_instants(bounds: list[str]) -> None:
    parsed = [datetime.fromisoformat(value) for value in bounds]
    if any(value.tzinfo is None for value in parsed):
        raise ValueError("instant temporal bounds require explicit timezone")
    if len(parsed) == 2 and parsed[0] > parsed[1]:
        raise ValueError("valid_from cannot follow valid_to")


class ExtractionOutput(StrictModel):
    entities: tuple[ExtractedEntity, ...] = Field(default=(), max_length=100)
    assertions: tuple[ExtractedAssertion, ...] = Field(default=(), max_length=200)
    title: str = Field(default="", max_length=256)
    description: str = Field(default="", max_length=2000)
    retrieval_context: str = Field(default="", max_length=2000)
    title_evidence: tuple[EvidenceSpan, ...] = Field(default=(), max_length=32)
    description_evidence: tuple[EvidenceSpan, ...] = Field(default=(), max_length=32)
    retrieval_context_evidence: tuple[EvidenceSpan, ...] = Field(default=(), max_length=32)
    complete: bool = True
    overflow: bool = False
    ontology_gaps: tuple[str, ...] = Field(default=(), max_length=32)
    validation_repairs: int = Field(default=0, ge=0, le=8)
    rejected_output_count: int = Field(default=0, ge=0, le=8)
    rejection_reasons: tuple[str, ...] = Field(default=(), max_length=32)

    @model_validator(mode="after")
    def validate_references(self) -> Self:
        ids = {entity.local_id for entity in self.entities}
        if len(ids) != len(self.entities):
            raise ValueError("duplicate entity local IDs")
        if len({item.local_id for item in self.assertions}) != len(self.assertions):
            raise ValueError("duplicate assertion local IDs")
        if any(item.subject_id not in ids or item.object_id not in ids for item in self.assertions):
            raise ValueError("assertion references unknown entity local ID")
        return self

    def validate_evidence(self, value: ChunkExtractionInput) -> None:
        items: tuple[ExtractedEntity | ExtractedAssertion, ...] = (*self.entities, *self.assertions)
        for item in items:
            if item.span.source != "content":
                raise ValueError("entity/assertion evidence must cite target chunk content")
            item.span.validate_content(value.content)
        for entity in self.entities:
            if entity.description and not entity.description_evidence:
                raise ValueError("generated entity description requires grounded evidence spans")
            for span in entity.description_evidence:
                span.validate_content(getattr(value, span.source))
        for name in ("title", "description", "retrieval_context"):
            spans = getattr(self, name + "_evidence")
            if getattr(self, name) and not spans:
                raise ValueError("generated text requires grounded evidence spans")
            for span in spans:
                span.validate_content(getattr(value, span.source))

    def require_complete(self) -> None:
        if not self.complete or self.overflow:
            raise ExtractionIncompleteError(
                "extraction incomplete or overflow; publication is forbidden"
            )

    def validate_profile(self, profile: ExtractionProfile) -> None:
        self.require_complete()
        if profile.schema_version not in {"1", "2", "3", "4"}:
            raise ValueError("unsupported extraction schema version")
        if profile.schema_version != "1":
            profile.resolved_ontology().validate_output(self)
            required: tuple[str, ...] = ("title", "description", "retrieval_context")
            if profile.schema_version == "4":
                required = ("title", "description")
            if any(not getattr(self, name).strip() for name in required):
                raise ValueError("semantic extraction requires a title and description")
            if any(not assertion.statement_text.strip() for assertion in self.assertions):
                raise ValueError("v2 assertions require complete statement_text")
        if profile.schema_version in {"3", "4"} and any(
            not entity.description.strip() for entity in self.entities
        ):
            raise ValueError("v3 entities require grounded descriptions")
        if profile.schema_version == "4":
            self._validate_semantic_text()

    def _validate_semantic_text(self) -> None:
        enforce_text_budget(self.title, field="chunk title", max_words=CHUNK_TITLE_MAX_WORDS)
        enforce_text_budget(
            self.description,
            field="chunk description",
            max_words=CHUNK_DESCRIPTION_MAX_WORDS,
        )
        if self.retrieval_context or self.retrieval_context_evidence:
            raise ValueError("v4 uses the chunk description as embedding context")
        for entity in self.entities:
            enforce_entity_name(entity.name)
            enforce_text_budget(
                entity.description,
                field="entity description",
                max_words=ENTITY_DESCRIPTION_MAX_WORDS,
            )
        for assertion in self.assertions:
            enforce_text_budget(
                assertion.statement_text,
                field="relationship description",
                max_words=RELATION_DESCRIPTION_MAX_WORDS,
            )
