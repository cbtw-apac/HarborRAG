"""Strict loader for operator-authored LLM extraction vocabularies."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import ConfigDict, Field, ValidationError

from harborrag_core.base import StrictModel
from harborrag_core.topology.ontology import (
    OntologyName,
    OntologyRegistry,
    RelationDefinition,
)
from harborrag_runtime.config.errors import GraphBuildConfigurationError
from harborrag_runtime.config.loading import read_yaml_file, require_string_mapping

_ANY = "any"
type _Endpoints = Literal["any"] | list[OntologyName]


class _RelationAuthoring(StrictModel):
    """Author endpoint typing as subjects x objects instead of explicit pairs."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    name: OntologyName
    subjects: _Endpoints
    objects: _Endpoints
    examples: list[str] = Field(default_factory=list, max_length=16)
    exclusions: list[str] = Field(default_factory=list, max_length=16)

    def resolve(self, entity_types: tuple[str, ...]) -> RelationDefinition:
        subjects = entity_types if self.subjects == _ANY else self.subjects
        objects = entity_types if self.objects == _ANY else self.objects
        undefined = sorted({*subjects, *objects} - set(entity_types))
        if undefined:
            raise ValueError(
                f"relation {self.name!r} references undeclared entity types: {', '.join(undefined)}"
            )
        return RelationDefinition(
            name=self.name,
            endpoint_pairs=tuple((s, o) for s in subjects for o in objects),
            examples=tuple(self.examples),
            exclusions=tuple(self.exclusions),
        )


class _OntologyAuthoring(StrictModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    version: str = Field(min_length=1, max_length=128)
    entity_types: list[OntologyName] = Field(min_length=1, max_length=128)
    relations: list[_RelationAuthoring] = Field(min_length=1, max_length=128)

    def resolve(self) -> OntologyRegistry:
        types = tuple(self.entity_types)
        return OntologyRegistry(
            version=self.version,
            entity_types=types,
            relations=tuple(r.resolve(types) for r in self.relations),
        )


def load_ontology(path: str | Path) -> OntologyRegistry:
    """Decode one vocabulary file and expand it into the frozen runtime registry."""

    source, raw = read_yaml_file(path, label="Ontology", error_type=GraphBuildConfigurationError)
    root = require_string_mapping(
        raw, label="ontology root", error_type=GraphBuildConfigurationError
    )
    try:
        return _OntologyAuthoring.model_validate(root, strict=True).resolve()
    except (ValidationError, ValueError) as error:
        raise GraphBuildConfigurationError(f"Invalid ontology {source}: {error}") from error
