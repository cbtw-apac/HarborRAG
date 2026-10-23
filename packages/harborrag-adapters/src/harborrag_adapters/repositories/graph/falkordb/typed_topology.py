"""Typed incidence projection: facts stay canonical sidecars, not property nodes."""

import json
import re
from dataclasses import dataclass
from typing import Any

from harborrag_core.storage import StorageOperationContext
from harborrag_core.topology import DocumentTopologyBuild
from harborrag_core.topology.extraction import digest

from .client import FalkorDBClient
from .knowledge_support import read_rows
from .typed_topology_validation import validate_typed_build


@dataclass(frozen=True)
class TypedTopologyRows:
    nodes: tuple[dict[str, Any], ...]
    edges: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class TypedEdgeSpec:
    source: str
    target: str
    relation_type: str
    identity: str
    attributes: dict[str, Any]


class TypedTopologyBuilder:
    def build(self, build: DocumentTopologyBuild, tenant_id: str) -> TypedTopologyRows:
        validate_typed_build(build, tenant_id)
        common = {
            "tenant_id": tenant_id,
            "build_id": build.build_id,
            "document_id": build.document_id,
            "document_version_id": build.document_version_id,
            "config_epoch": build.config_epoch,
            "source_scope_id": build.source_scope_id,
            "projection_revision": build.projection_revision,
            "permission_revision_hash": digest(
                [item.model_dump(mode="json") for item in build.permission_dependencies]
            ),
            "artifact_sha256": build.artifact.sha256,
        }
        nodes: dict[str, dict[str, Any]] = {}
        representations = {item.chunk_id: item for item in build.representations}
        for chunk_id in build.chunk_ids:
            key = f"chunk:{chunk_id}"
            row = {**common, "record_key": key, "kind": "chunk", "chunk_id": chunk_id}
            representation = representations.get(chunk_id)
            if representation is not None:
                row.update(
                    title=representation.title,
                    description=representation.description,
                    generated_title=representation.title,
                    generated_description=representation.description,
                    retrieval_context=representation.retrieval_context,
                )
            nodes[key] = row
        edges: list[dict[str, Any]] = []
        for mention in sorted(build.mentions, key=lambda item: item.mention_id):
            key = f"entity:{mention.entity_id}"
            # The tenant identity is separate from this build/permission-specific view.
            previous = nodes.setdefault(
                key,
                {
                    **common,
                    "record_key": key,
                    "kind": "entity_view",
                    "entity_id": mention.entity_id,
                    "title": mention.observation.name,
                    **(
                        {
                            "description": mention.observation.description,
                            "generated_description": mention.observation.description,
                        }
                        if mention.observation.description
                        else {}
                    ),
                    "entity_type": mention.observation.entity_type,
                },
            )
            if previous["entity_type"] != mention.observation.entity_type:
                raise ValueError("entity view has incompatible supported types")
            edges.append(
                self._edge(
                    common,
                    TypedEdgeSpec(
                        f"chunk:{mention.chunk_id}",
                        key,
                        "MENTIONS",
                        mention.mention_id,
                        {
                            "mention_id": mention.mention_id,
                            "start": mention.observation.span.start,
                            "end": mention.observation.span.end,
                        },
                    ),
                )
            )
        for assertion in build.assertions:
            item = assertion.observation
            edges.append(
                self._edge(
                    common,
                    TypedEdgeSpec(
                        f"entity:{assertion.subject_entity_id}",
                        f"entity:{assertion.object_entity_id}",
                        item.predicate.upper(),
                        assertion.assertion_id,
                        {
                            "assertion_id": assertion.assertion_id,
                            "chunk_id": assertion.chunk_id,
                            "polarity": item.polarity,
                            "modality": item.modality,
                            "attribution": item.attribution or "",
                            "valid_from": item.valid_from or "",
                            "valid_to": item.valid_to or "",
                            "temporal_precision": item.temporal_precision,
                            "time_qualifier": item.time_qualifier or "",
                            "qualifiers": json.dumps(item.qualifiers),
                            "start": item.span.start,
                            "end": item.span.end,
                        },
                    ),
                )
            )
        if any(edge[key] not in nodes for edge in edges for key in ("source", "target")):
            raise ValueError("typed graph relation lacks a supported endpoint")
        return TypedTopologyRows(
            tuple(nodes[key] for key in sorted(nodes)),
            tuple(sorted(edges, key=lambda row: row["relation_key"])),
        )

    @staticmethod
    def _edge(common: dict[str, Any], spec: TypedEdgeSpec) -> dict[str, Any]:
        if re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", spec.relation_type) is None:
            raise ValueError("invalid schema relationship identifier")
        return {
            **common,
            "source": spec.source,
            "target": spec.target,
            "relation_type": spec.relation_type,
            "relation_key": digest([spec.identity, spec.source, spec.target, spec.relation_type]),
            **spec.attributes,
        }


class TypedTopologyProjection:
    def __init__(self, database: FalkorDBClient) -> None:
        self._database = database
        self._builder = TypedTopologyBuilder()

    async def write(
        self, build: DocumentTopologyBuild, *, context: StorageOperationContext
    ) -> None:
        rows = self._builder.build(build, str(context.tenant_id))
        for start in range(0, len(rows.nodes), 250):
            await self._database.write(
                "UNWIND $rows AS row MERGE (n:TopologyRecord {tenant_id: row.tenant_id, "
                "build_id: row.build_id, record_key: row.record_key}) SET n = row",
                {"rows": rows.nodes[start : start + 250]},
            )
        for relation_type in sorted({row["relation_type"] for row in rows.edges}):
            selected = [row for row in rows.edges if row["relation_type"] == relation_type]
            for start in range(0, len(selected), 250):
                batch = [
                    {
                        **row,
                        "properties": {
                            key: value
                            for key, value in row.items()
                            if key not in {"source", "target", "relation_type"}
                        },
                    }
                    for row in selected[start : start + 250]
                ]
                await self._database.write(
                    "UNWIND $rows AS row MATCH (s:TopologyRecord {tenant_id: row.tenant_id, "
                    "build_id: row.build_id, record_key: row.source}), "
                    "(t:TopologyRecord {tenant_id: row.tenant_id, build_id: row.build_id, record_key: row.target}) "
                    f"MERGE (s)-[r:{relation_type} {{tenant_id: row.tenant_id, build_id: row.build_id, "
                    "relation_key: row.relation_key}]->(t) SET r = row.properties",
                    {"rows": batch},
                )

    async def verify(
        self, build: DocumentTopologyBuild, *, context: StorageOperationContext
    ) -> bool:
        rows = self._builder.build(build, str(context.tenant_id))
        params = {"tenant_id": str(context.tenant_id), "build_id": build.build_id}
        nodes = await read_rows(
            self._database,
            "MATCH (n:TopologyRecord {tenant_id: $tenant_id, build_id: $build_id}) RETURN properties(n) AS properties",
            params,
        )
        edges = await read_rows(
            self._database,
            "MATCH (s)-[r {tenant_id: $tenant_id, build_id: $build_id}]->(t) "
            "RETURN properties(r) AS properties, type(r) AS relation_type, "
            "s.record_key AS source, t.record_key AS target, s.tenant_id AS source_tenant, "
            "t.tenant_id AS target_tenant, s.build_id AS source_build, t.build_id AS target_build",
            params,
        )
        expected = [
            {
                "properties": {
                    key: value
                    for key, value in row.items()
                    if key not in {"source", "target", "relation_type"}
                },
                **{key: row[key] for key in ("source", "target", "relation_type")},
                "source_tenant": str(context.tenant_id),
                "target_tenant": str(context.tenant_id),
                "source_build": build.build_id,
                "target_build": build.build_id,
            }
            for row in rows.edges
        ]
        try:
            return (
                len(nodes) == len(rows.nodes)
                and len(edges) == len(expected)
                and {digest(node["properties"]) for node in nodes}
                == {digest(node) for node in rows.nodes}
                and {digest(edge) for edge in edges} == {digest(edge) for edge in expected}
            )
        except (KeyError, TypeError, ValueError):
            return False
