"""Deterministic row builder for the compact semantic FalkorDB projection."""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Any

from harborrag_core.topology import DocumentTopologyBuild
from harborrag_core.topology.extraction import digest

from .typed_topology_validation import validate_typed_build


@dataclass(frozen=True)
class UnifiedTopologyRows:
    chunks: tuple[dict[str, Any], ...]
    entities: tuple[dict[str, Any], ...]
    edges: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class UnifiedEdgeSpec:
    source: str
    target: str
    relation_type: str
    source_kind: str
    attributes: dict[str, Any]


@dataclass(frozen=True)
class SemanticEntitySpec:
    identity: str
    name: str
    entity_type: str
    description: str
    aliases: list[str]
    support_count: int


class UnifiedTopologyBuilder:
    """Build one entity view and one directed typed edge per logical adjacency."""

    def build(self, build: DocumentTopologyBuild, tenant_id: str) -> UnifiedTopologyRows:
        validate_typed_build(build, tenant_id)
        properties = SemanticPropertyFactory(build, tenant_id)
        representations = {item.chunk_id: item for item in build.representations}
        chunks = tuple(
            properties.chunk(
                chunk_id,
                name=representations[chunk_id].title,
                description=representations[chunk_id].description,
            )
            for chunk_id in build.chunk_ids
        )
        entities = self._entities(build, properties)
        mentions = self._mentions(build, properties)
        assertions = self._assertions(build, properties)
        return UnifiedTopologyRows(
            chunks,
            tuple(entities[key] for key in sorted(entities)),
            tuple(sorted((*mentions, *assertions), key=lambda row: row["relation_key"])),
        )

    @staticmethod
    def _entities(
        build: DocumentTopologyBuild, properties: SemanticPropertyFactory
    ) -> dict[str, dict[str, Any]]:
        grouped = defaultdict(list)
        for mention in build.mentions:
            grouped[mention.entity_id].append(mention)
        rows: dict[str, dict[str, Any]] = {}
        for entity_id, mentions in grouped.items():
            types = {item.observation.entity_type for item in mentions}
            if len(types) != 1:
                raise ValueError("entity view has incompatible supported types")
            descriptions = tuple(
                sorted(
                    {
                        item.observation.description.strip()
                        for item in mentions
                        if item.observation.description.strip()
                    }
                )
            )
            if not descriptions:
                raise ValueError("semantic-v6 entity view requires a description")
            counts = Counter(
                item.observation.description.strip()
                for item in mentions
                if item.observation.description.strip()
            )
            highest_support = max(counts.values())
            # Projection remains pure: choose the shortest most-supported frozen
            # description. A separate accepted reducer may later synthesize one.
            description = min(
                (value for value, count in counts.items() if count == highest_support),
                key=lambda value: (len(value), value.casefold(), value),
            )
            selected = min(
                mentions,
                key=lambda item: (
                    item.observation.name.casefold(),
                    item.observation.name,
                    item.mention_id,
                ),
            )
            key = f"entity:{entity_id}"
            aliases = sorted(
                {
                    alias
                    for item in mentions
                    for alias in (item.observation.name, *item.observation.aliases)
                },
                key=lambda value: (value.casefold(), value),
            )
            rows[key] = properties.entity(
                SemanticEntitySpec(
                    identity=key,
                    name=selected.observation.name,
                    entity_type=selected.observation.entity_type,
                    description=description,
                    aliases=aliases,
                    support_count=len(mentions),
                )
            )
        return rows

    @classmethod
    def _mentions(
        cls, build: DocumentTopologyBuild, properties: SemanticPropertyFactory
    ) -> tuple[dict[str, Any], ...]:
        grouped = defaultdict(list)
        for mention in build.mentions:
            grouped[(mention.chunk_id, mention.entity_id)].append(mention)
        rows = []
        for (chunk_id, entity_id), mentions in grouped.items():
            rows.append(
                cls._edge(
                    properties,
                    UnifiedEdgeSpec(
                        source=chunk_id,
                        target=f"entity:{entity_id}",
                        relation_type="MENTIONS",
                        source_kind="chunk",
                        attributes={
                            "support_count": len(mentions),
                        },
                    ),
                )
            )
        return tuple(rows)

    @classmethod
    def _assertions(
        cls, build: DocumentTopologyBuild, properties: SemanticPropertyFactory
    ) -> tuple[dict[str, Any], ...]:
        grouped = defaultdict(list)
        for assertion in build.assertions:
            grouped[(assertion.subject_entity_id, assertion.object_entity_id)].append(assertion)
        rows = []
        for (subject, object_), assertions in grouped.items():
            claims = {_claim_tuple(item) for item in assertions}
            relation_types = sorted({item.observation.predicate.upper() for item in assertions})
            conflicts = sum(
                len(
                    {
                        item.observation.polarity
                        for item in assertions
                        if item.observation.predicate == predicate
                    }
                )
                > 1
                for predicate in {item.observation.predicate for item in assertions}
            )
            descriptions = sorted({claim[3] for claim in claims})
            if len(claims) == 1:
                description = descriptions[0]
            elif conflicts:
                description = (
                    f"{len(claims)} evidence-backed claims conflict for this entity pair; "
                    "inspect canonical evidence and qualifiers."
                )
            else:
                description = (
                    f"{len(claims)} evidence-backed claims connect this entity pair; "
                    "inspect canonical evidence for predicates and qualifiers."
                )
            rows.append(
                cls._edge(
                    properties,
                    UnifiedEdgeSpec(
                        source=f"entity:{subject}",
                        target=f"entity:{object_}",
                        # Exactly one directed semantic adjacency per entity pair. The
                        # ontology predicates remain queryable properties, matching the
                        # FalkorDB SDK's RELATES + rel_type design.
                        relation_type="RELATES",
                        source_kind="entity",
                        attributes={
                            "description": description,
                            "types": relation_types,
                            "support_count": len(assertions),
                            "claim_count": len(claims),
                            "conflict_count": conflicts,
                            "has_conflicts": bool(conflicts),
                        },
                    ),
                )
            )
        return tuple(rows)

    @staticmethod
    def _edge(properties: SemanticPropertyFactory, spec: UnifiedEdgeSpec) -> dict[str, Any]:
        if re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", spec.relation_type) is None:
            raise ValueError("invalid schema relationship identifier")
        return properties.edge(spec)


def _claim_tuple(assertion: Any) -> tuple[Any, ...]:
    """Keep every qualifier correlated while computing projection-only aggregates."""

    value = assertion.observation
    return (
        value.predicate,
        value.polarity,
        value.modality,
        value.statement_text.strip(),
        value.attribution,
        value.qualifiers,
        value.time_qualifier,
        value.valid_from,
        value.valid_to,
        value.temporal_precision,
    )


@dataclass(frozen=True)
class SemanticPropertyFactory:
    """Own the deliberately small, user-facing semantic graph contract."""

    build: DocumentTopologyBuild
    tenant_id: str

    def chunk(self, chunk_id: str, *, name: str, description: str) -> dict[str, Any]:
        return {
            "name": name,
            "description": description,
            "chunk_id": chunk_id,
            "tenant_id": self.tenant_id,
            "build_id": self.build.build_id,
            "document_version_id": self.build.document_version_id,
        }

    def entity(self, spec: SemanticEntitySpec) -> dict[str, Any]:
        return {
            "name": spec.name,
            "type": spec.entity_type,
            "description": spec.description,
            "aliases": spec.aliases,
            "support_count": spec.support_count,
            "id": spec.identity,
            "tenant_id": self.tenant_id,
            "build_id": self.build.build_id,
        }

    def edge(self, spec: UnifiedEdgeSpec) -> dict[str, Any]:
        identity = digest([self.build.build_id, spec.source, spec.target, spec.relation_type])
        return {
            **spec.attributes,
            "id": identity,
            "tenant_id": self.tenant_id,
            "build_id": self.build.build_id,
            "document_version_id": self.build.document_version_id,
            "source": spec.source,
            "target": spec.target,
            "source_kind": spec.source_kind,
            "relation_type": spec.relation_type,
            # Builder-only sort alias; the projector never persists it.
            "relation_key": identity,
        }
