"""Frozen application-owned ontology; extracted names never expand the schema."""

from __future__ import annotations

from typing import TYPE_CHECKING, Self

from pydantic import Field, model_validator

from harborrag_core.base import StrictModel

if TYPE_CHECKING:
    from .extraction import ExtractionOutput


class RelationDefinition(StrictModel):
    name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    endpoint_pairs: tuple[tuple[str, str], ...] = Field(min_length=1, max_length=256)
    direction: str = "subject_to_object"
    examples: tuple[str, ...] = Field(default=(), max_length=16)
    exclusions: tuple[str, ...] = Field(default=(), max_length=16)


class OntologyRegistry(StrictModel):
    version: str = Field(min_length=1, max_length=128)
    entity_types: tuple[str, ...] = Field(min_length=1, max_length=128)
    relations: tuple[RelationDefinition, ...] = Field(min_length=1, max_length=128)

    @model_validator(mode="after")
    def validate_registry(self) -> Self:
        types = set(self.entity_types)
        names = {relation.name for relation in self.relations}
        if len(types) != len(self.entity_types) or len(names) != len(self.relations):
            raise ValueError("ontology names must be unique")
        for relation in self.relations:
            if relation.direction != "subject_to_object":
                raise ValueError("ontology direction must be subject_to_object")
            if any(endpoint not in types for pair in relation.endpoint_pairs for endpoint in pair):
                raise ValueError("ontology endpoint type is undefined")
        return self

    def validate_output(self, output: ExtractionOutput) -> None:
        entities = {entity.local_id: entity for entity in output.entities}
        definitions = {relation.name: relation for relation in self.relations}
        if any(entity.entity_type not in self.entity_types for entity in output.entities):
            raise ValueError("extracted entity type is outside the configured ontology")
        for assertion in output.assertions:
            definition = definitions.get(assertion.predicate)
            if definition is None:
                raise ValueError("extracted predicate is outside the configured ontology")
            pair = (
                entities[assertion.subject_id].entity_type,
                entities[assertion.object_id].entity_type,
            )
            if pair not in definition.endpoint_pairs:
                raise ValueError("assertion endpoint types violate the configured ontology")


def builtin_ontology() -> OntologyRegistry:
    """Conservative starter schema, not a claim of an existing enterprise registry."""
    types = (
        "person",
        "team",
        "organization",
        "service",
        "system",
        "component",
        "document",
        "project",
        "policy",
        "product",
    )
    technical = ("service", "system", "component", "project", "product")
    return OntologyRegistry(
        version="builtin-enterprise-v1",
        entity_types=types,
        relations=(
            RelationDefinition(
                name="owns",
                endpoint_pairs=tuple(
                    (owner, item)
                    for owner in ("person", "team", "organization")
                    for item in (*technical, "document", "policy")
                ),
                examples=("Team A owns Service B.",),
                exclusions=("Proximity or shared names do not establish ownership.",),
            ),
            RelationDefinition(
                name="depends_on",
                endpoint_pairs=tuple((a, b) for a in technical for b in technical),
                examples=("Service A depends on Service B.",),
                exclusions=("Co-occurrence does not establish a dependency.",),
            ),
            RelationDefinition(
                name="supersedes",
                endpoint_pairs=tuple((kind, kind) for kind in types),
                examples=("Policy B supersedes Policy A.",),
                exclusions=("A newer observation is not necessarily a replacement.",),
            ),
            RelationDefinition(
                name="related_to",
                endpoint_pairs=tuple((a, b) for a in types for b in types),
                examples=("The document explicitly relates project A to policy B.",),
                exclusions=("Shared passage membership alone is not a relationship.",),
            ),
        ),
    )
