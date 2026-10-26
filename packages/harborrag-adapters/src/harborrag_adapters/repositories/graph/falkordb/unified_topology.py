"""Semantic-v6 writer/verifier for unified structural and semantic topology."""

from __future__ import annotations

from typing import Any

from harborrag_core.storage import StorageOperationContext
from harborrag_core.topology import DocumentTopologyBuild
from harborrag_core.topology.extraction import digest

from .client import FalkorDBClient
from .knowledge_support import read_rows
from .unified_topology_builder import UnifiedTopologyBuilder


class UnifiedTopologyProjection:
    def __init__(self, database: FalkorDBClient) -> None:
        self._database = database
        self._builder = UnifiedTopologyBuilder()

    async def write(
        self, build: DocumentTopologyBuild, *, context: StorageOperationContext
    ) -> None:
        rows = self._builder.build(build, str(context.tenant_id))
        await self._remove_legacy_semantic_rows(build, str(context.tenant_id))
        await self._write_chunks(rows.chunks)
        await self._write_supported_structures(build, str(context.tenant_id))
        await self._write_entities(rows.entities)
        await self._write_edges(rows.edges)

    async def _write_chunks(self, rows: tuple[dict[str, Any], ...]) -> None:
        for start in range(0, len(rows), 250):
            await self._database.write(
                "UNWIND $rows AS row MATCH (chunk:KnowledgeNode:Chunk {tenant_id: row.tenant_id, "
                "node_key: row.chunk_id}) WHERE chunk.document_version_id = row.document_version_id "
                "SET chunk.description = row.description, "
                "chunk.name = row.name, "
                "chunk.title = row.name, "
                "chunk.title_key = toLower(row.name), "
                "chunk.description_build_id = row.build_id",
                {"rows": rows[start : start + 250]},
            )

    async def _write_supported_structures(
        self, build: DocumentTopologyBuild, tenant_id: str
    ) -> None:
        """Copy an LLM description onto a leaf structure with exactly one evidence chunk."""
        await self._database.write(
            "MATCH (structure:KnowledgeNode:Structure)-[:HAS_CHUNK]->"
            "(chunk:KnowledgeNode:Chunk) "
            "WHERE chunk.tenant_id = $tenant_id AND structure.tenant_id = $tenant_id "
            "AND chunk.document_version_id = $document_version_id "
            "AND structure.document_version_id = $document_version_id "
            "AND structure.entity_type = 'table' "
            "WITH structure, collect(chunk) AS children WHERE size(children) = 1 "
            "SET structure.description = head(children).description, "
            "structure.description_build_id = $build_id "
            "REMOVE structure.generated_description",
            {
                "tenant_id": tenant_id,
                "document_version_id": build.document_version_id,
                "build_id": build.build_id,
            },
        )

    async def _write_entities(self, rows: tuple[dict[str, Any], ...]) -> None:
        for start in range(0, len(rows), 250):
            await self._database.write(
                "UNWIND $rows AS row MERGE (n:TopologyRecord:Entity {tenant_id: row.tenant_id, "
                "build_id: row.build_id, id: row.id}) SET n = row",
                {"rows": rows[start : start + 250]},
            )

    async def _remove_legacy_semantic_rows(
        self, build: DocumentTopologyBuild, tenant_id: str
    ) -> None:
        await self._database.write(
            "MATCH (n:TopologyRecord {tenant_id: $tenant_id, build_id: $build_id}) "
            "WHERE n.record_key IS NOT NULL DETACH DELETE n",
            {"tenant_id": tenant_id, "build_id": build.build_id},
        )

    async def _write_edges(self, rows: tuple[dict[str, Any], ...]) -> None:
        for relation_type in sorted({row["relation_type"] for row in rows}):
            for source_kind in ("chunk", "entity"):
                selected = tuple(
                    row
                    for row in rows
                    if row["relation_type"] == relation_type and row["source_kind"] == source_kind
                )
                for start in range(0, len(selected), 250):
                    await self._database.write(
                        self._edge_statement(relation_type, source_kind),
                        {"rows": [_edge_parameters(row) for row in selected[start : start + 250]]},
                    )

    @staticmethod
    def _edge_statement(relation_type: str, source_kind: str) -> str:
        source = (
            "MATCH (s:KnowledgeNode:Chunk {tenant_id: row.tenant_id, node_key: row.source}) "
            "WHERE s.document_version_id = row.document_version_id "
            if source_kind == "chunk"
            else "MATCH (s:TopologyRecord:Entity {tenant_id: row.tenant_id, build_id: row.build_id, id: row.source}) "
        )
        return (
            "UNWIND $rows AS row "
            + source
            + "MATCH (t:TopologyRecord:Entity {tenant_id: row.tenant_id, build_id: row.build_id, id: row.target}) "
            + f"MERGE (s)-[r:{relation_type} {{tenant_id: row.tenant_id, build_id: row.build_id, "
            + "id: row.id}]->(t) SET r = row.properties"
        )

    async def verify(
        self, build: DocumentTopologyBuild, *, context: StorageOperationContext
    ) -> bool:
        rows = self._builder.build(build, str(context.tenant_id))
        params = {"tenant_id": str(context.tenant_id), "build_id": build.build_id}
        chunks = await read_rows(
            self._database,
            "MATCH (n:KnowledgeNode:Chunk) WHERE n.tenant_id = $tenant_id "
            "AND n.description_build_id = $build_id RETURN n.node_key AS chunk_id, "
            "n.name AS name, n.description AS description",
            params,
        )
        structures = await read_rows(
            self._database,
            "MATCH (structure:KnowledgeNode:Structure)-[support:HAS_CHUNK]->"
            "(chunk:KnowledgeNode:Chunk) "
            "WHERE chunk.tenant_id = $tenant_id AND structure.tenant_id = $tenant_id "
            "AND chunk.document_version_id = $document_version_id "
            "AND structure.document_version_id = $document_version_id "
            "AND structure.entity_type = 'table' "
            "WITH structure, collect(chunk) AS children, count(support) AS occurrences "
            "RETURN size(children) AS child_count, head(children).description AS child_description, "
            "structure.description AS structure_description, "
            "structure.description_build_id AS description_build_id, occurrences",
            {
                **params,
                "document_version_id": build.document_version_id,
            },
        )
        entities = await read_rows(
            self._database,
            "MATCH (n:TopologyRecord:Entity {tenant_id: $tenant_id, build_id: $build_id}) "
            "RETURN properties(n) AS properties",
            params,
        )
        edges = await read_rows(
            self._database,
            "MATCH (s)-[r {tenant_id: $tenant_id, build_id: $build_id}]->"
            "(t:TopologyRecord) RETURN properties(r) AS properties, type(r) AS relation_type, "
            "coalesce(s.node_key, s.id) AS source, t.id AS target",
            params,
        )
        expected_chunks = {
            digest({key: row[key] for key in ("chunk_id", "name", "description")})
            for row in rows.chunks
        }
        expected_edges = {_edge_digest(row) for row in rows.edges}
        try:
            return (
                len(chunks) == len(rows.chunks)
                and {digest(row) for row in chunks} == expected_chunks
                and all(
                    int(row["child_count"]) == 1
                    and int(row["occurrences"]) == 1
                    and row["child_description"] == row["structure_description"]
                    and row["description_build_id"] == build.build_id
                    for row in structures
                )
                and len(entities) == len(rows.entities)
                and {digest(row["properties"]) for row in entities}
                == {digest(row) for row in rows.entities}
                and len(edges) == len(rows.edges)
                and {_edge_digest_from_read(row) for row in edges} == expected_edges
            )
        except (KeyError, TypeError, ValueError):
            return False


def _edge_parameters(row: dict[str, Any]) -> dict[str, Any]:
    return {
        **row,
        "properties": {
            key: value
            for key, value in row.items()
            if key
            not in {
                "source",
                "target",
                "source_kind",
                "relation_type",
                "relation_key",
                "document_version_id",
            }
        },
    }


def _edge_digest(row: dict[str, Any]) -> str:
    return digest(
        {
            "source": row["source"],
            "target": row["target"],
            "relation_type": row["relation_type"],
            "properties": _edge_parameters(row)["properties"],
        }
    )


def _edge_digest_from_read(row: dict[str, Any]) -> str:
    return digest(
        {
            "source": row["source"],
            "target": row["target"],
            "relation_type": row["relation_type"],
            "properties": row["properties"],
        }
    )
