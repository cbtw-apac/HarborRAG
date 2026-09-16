"""Generate a strict per-ontology response schema without model-defined types."""

from types import GenericAlias
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, create_model

from harborrag_core.topology.extraction import (
    EvidenceSpan,
    ExtractedAssertion,
    ExtractedEntity,
    ExtractionOutput,
)
from harborrag_core.topology.ontology import OntologyRegistry
from harborrag_core.topology.text_policy import (
    CHUNK_DESCRIPTION_MAX_CHARS,
    CHUNK_TITLE_MAX_CHARS,
    ENTITY_DESCRIPTION_MAX_CHARS,
    ENTITY_NAME_MAX_CHARS,
    RELATION_DESCRIPTION_MAX_CHARS,
)


def response_schema(registry: OntologyRegistry, *, schema_version: str = "4") -> type[BaseModel]:
    entity_enum: Any = Literal[tuple(registry.entity_types)]
    relation_enum: Any = Literal[tuple(relation.name for relation in registry.relations)]
    content_evidence = create_model(
        "OntologyContentEvidenceSpan",
        __base__=EvidenceSpan,
        source=(Literal["content"], ...),
    )
    supporting_evidence = create_model(
        "OntologySupportingEvidenceSpan",
        __base__=EvidenceSpan,
        source=(Literal["content", "context", "source_title"], ...),
    )
    entity = create_model(
        "OntologyEntity",
        __base__=ExtractedEntity,
        name=(str, Field(min_length=2, max_length=ENTITY_NAME_MAX_CHARS)),
        entity_type=(entity_enum, ...),
        description=(str, Field(min_length=1, max_length=ENTITY_DESCRIPTION_MAX_CHARS)),
        description_evidence=(
            GenericAlias(tuple, (supporting_evidence, Ellipsis)),
            Field(min_length=1, max_length=32),
        ),
        span=(content_evidence, ...),
        aliases=(tuple[str, ...], Field(max_length=16)),
        external_id=(str | None, ...),
    )
    assertion = create_model(
        "OntologyAssertion",
        __base__=ExtractedAssertion,
        predicate=(relation_enum, ...),
        span=(content_evidence, ...),
        polarity=(Literal["affirmative", "negative"], ...),
        modality=(Literal["asserted", "possible", "required"], ...),
        time_qualifier=(str | None, Field(max_length=256)),
        statement_text=(
            str,
            Field(min_length=1, max_length=RELATION_DESCRIPTION_MAX_CHARS),
        ),
        attribution=(str | None, Field(max_length=1000)),
        qualifiers=(tuple[str, ...], Field(max_length=32)),
        valid_from=(str | None, Field(max_length=64)),
        valid_to=(str | None, Field(max_length=64)),
        temporal_precision=(
            Literal["unknown", "year", "month", "day", "instant", "interval", "relative"],
            ...,
        ),
    )
    common_fields: dict[str, Any] = {
        "entities": (GenericAlias(tuple, (entity, Ellipsis)), Field(max_length=100)),
        "assertions": (GenericAlias(tuple, (assertion, Ellipsis)), Field(max_length=200)),
        "title": (str, Field(min_length=1, max_length=CHUNK_TITLE_MAX_CHARS)),
        "description": (str, Field(min_length=1, max_length=CHUNK_DESCRIPTION_MAX_CHARS)),
        "title_evidence": (
            GenericAlias(tuple, (supporting_evidence, Ellipsis)),
            Field(min_length=1, max_length=32),
        ),
        "description_evidence": (
            GenericAlias(tuple, (supporting_evidence, Ellipsis)),
            Field(min_length=1, max_length=32),
        ),
        "complete": (bool, ...),
        "overflow": (bool, ...),
        "ontology_gaps": (tuple[str, ...], Field(max_length=32)),
    }
    if schema_version == "4":
        # The v4 wire contract intentionally omits the retired retrieval-context
        # fields. ExtractionOutput supplies their empty compatibility defaults
        # after the provider response has been parsed.
        return create_model(
            "FusedExtractionV4",
            __config__=ConfigDict(extra="forbid"),
            **common_fields,
        )
    return create_model(
        "FusedExtractionV3",
        __base__=ExtractionOutput,
        entities=(GenericAlias(tuple, (entity, Ellipsis)), Field(max_length=100)),
        assertions=(GenericAlias(tuple, (assertion, Ellipsis)), Field(max_length=200)),
        title=(str, Field(min_length=1, max_length=CHUNK_TITLE_MAX_CHARS)),
        description=(str, Field(min_length=1, max_length=CHUNK_DESCRIPTION_MAX_CHARS)),
        retrieval_context=(str, Field(default="", max_length=2000)),
        title_evidence=(
            GenericAlias(tuple, (supporting_evidence, Ellipsis)),
            Field(min_length=1, max_length=32),
        ),
        description_evidence=(
            GenericAlias(tuple, (supporting_evidence, Ellipsis)),
            Field(min_length=1, max_length=32),
        ),
        retrieval_context_evidence=(
            GenericAlias(tuple, (supporting_evidence, Ellipsis)),
            Field(default=(), max_length=32),
        ),
        complete=(bool, ...),
        overflow=(bool, ...),
        ontology_gaps=(tuple[str, ...], Field(max_length=32)),
    )
