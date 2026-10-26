"""Project parent summaries directly onto their structural graph nodes."""

from typing import Any

from harborrag_core.storage import StorageOperationContext
from harborrag_core.topology import DocumentTopologyBuild
from harborrag_core.topology.derived import ParentDescription
from harborrag_core.topology.text_policy import (
    PARENT_DESCRIPTION_MAX_CHARS,
    PARENT_DESCRIPTION_MAX_WORDS,
    enforce_text_budget,
)

from .client import FalkorDBClient
from .knowledge_support import read_rows
from .typed_topology_validation import validate_typed_build


class ParentViewBuilder:
    def build(
        self, build: DocumentTopologyBuild, parents: tuple[ParentDescription, ...], tenant_id: str
    ) -> tuple[dict[str, Any], ...]:
        validate_typed_build(build, tenant_id)
        if (
            not tenant_id.strip()
            or not build.build_id.strip()
            or not build.document_version_id.strip()
        ):
            raise ValueError("parent views require explicit accepted source/build ownership")
        if len(parents) > 10000 or len({parent.parent_key for parent in parents}) != len(parents):
            raise ValueError("parent view manifest has duplicate identities or exceeds limits")
        available = set(build.chunk_ids)
        for parent in parents:
            self._validate_parent(parent, available)
        return tuple(
            {
                "name": (
                    parent.section_path[-1] if parent.level == "section" else "Document summary"
                ),
                "level": parent.level,
                "description": parent.description,
                "id": parent.parent_key,
                "section_path": list(parent.section_path),
                "structure_id": parent.structure_id,
                "tenant_id": tenant_id,
                "build_id": build.build_id,
                "document_id": build.document_id,
                "document_version_id": build.document_version_id,
                "input_digest": parent.input_digest,
                "input_coverage": parent.input_coverage,
                "semantic_coverage": parent.semantic_coverage,
            }
            for parent in sorted(parents, key=lambda item: item.parent_key)
        )

    @staticmethod
    def _validate_parent(parent: ParentDescription, available: set[str]) -> None:
        if not parent.complete or parent.level not in {"section", "structure", "document"}:
            raise ValueError("only complete section/document parent views can be projected")
        if parent.level != "structure" and (parent.level == "section") != bool(parent.section_path):
            raise ValueError("section parent views require one non-empty hierarchy path")
        if parent.level in {"section", "structure"} and parent.structure_id is None:
            raise ValueError("section parent views require a stable structure identity")
        inputs, citations = set(parent.input_chunk_ids), set(parent.cited_chunk_ids)
        if not parent.parent_key.strip() or not parent.input_digest.strip():
            raise ValueError("parent views require persistent keys and input digests")
        if not inputs <= available or not citations <= inputs:
            raise ValueError("parent view lineage or citations exceed accepted build evidence")
        if len(parent.description) > PARENT_DESCRIPTION_MAX_CHARS:
            raise ValueError("parent description exceeds graph character budget")
        enforce_text_budget(
            parent.description,
            field="parent description",
            max_words=PARENT_DESCRIPTION_MAX_WORDS,
        )


class ParentViewProjection:
    def __init__(self, database: FalkorDBClient) -> None:
        self._database = database
        self._builder = ParentViewBuilder()

    async def write(
        self,
        build: DocumentTopologyBuild,
        parents: tuple[ParentDescription, ...],
        *,
        context: StorageOperationContext,
    ) -> None:
        rows = self._builder.build(build, parents, str(context.tenant_id))
        # semantic-v3 used duplicate TopologyParentView nodes.  Parent descriptions are
        # views over real document/section nodes, so keep their lineage in canonical
        # storage and remove the graph-only wrapper during upgrade/rebuild.
        await self._database.write(
            "MATCH (view:TopologyParentView {tenant_id: $tenant_id, build_id: $build_id}) "
            "DETACH DELETE view",
            {"tenant_id": str(context.tenant_id), "build_id": build.build_id},
        )
        targets = (
            ("structure", "Structure", "target.logical_id = row.structure_id"),
            (
                "document",
                "DocumentVersion",
                "target.document_id = row.document_id",
            ),
            (
                "section",
                "Structure",
                "target.entity_type = 'section' AND target.logical_id = row.structure_id",
            ),
        )
        for level, label, predicate in targets:
            selected = tuple(row for row in rows if row["level"] == level)
            for start in range(0, len(selected), 250):
                await self._database.write(
                    "UNWIND $rows AS row "
                    f"MATCH (target:KnowledgeNode:{label}) WHERE target.tenant_id = row.tenant_id "
                    "AND target.document_version_id = row.document_version_id AND "
                    f"{predicate} "
                    "SET target.description = row.description, "
                    "target.description_build_id = row.build_id, "
                    "target.summary_input_coverage = row.input_coverage, "
                    "target.summary_semantic_coverage = row.semantic_coverage "
                    "REMOVE target.generated_description",
                    {"rows": selected[start : start + 250]},
                )

    async def verify(
        self,
        build: DocumentTopologyBuild,
        parents: tuple[ParentDescription, ...],
        *,
        context: StorageOperationContext,
    ) -> bool:
        rows = self._builder.build(build, parents, str(context.tenant_id))
        bindings: list[dict[str, Any]] = []
        targets = (
            ("structure", "Structure", "target.logical_id = row.structure_id"),
            ("document", "DocumentVersion", "target.document_id = row.document_id"),
            (
                "section",
                "Structure",
                "target.entity_type = 'section' AND target.logical_id = row.structure_id",
            ),
        )
        for level, label, predicate in targets:
            selected = tuple(row for row in rows if row["level"] == level)
            if not selected:
                continue
            bindings.extend(
                await read_rows(
                    self._database,
                    "UNWIND $rows AS row "
                    f"OPTIONAL MATCH (target:KnowledgeNode:{label}) "
                    "WHERE target.tenant_id = row.tenant_id "
                    "AND target.document_version_id = row.document_version_id AND "
                    f"{predicate} "
                    "RETURN row.id AS id, row.description AS expected_description, "
                    "collect(target.description) AS descriptions, count(target) AS occurrences",
                    {"rows": selected},
                )
            )
        try:
            expected = {str(row["id"]): str(row["description"]) for row in rows}
            linked = {
                str(row["id"]): str(row["expected_description"])
                for row in bindings
                if row.get("descriptions") == [row.get("expected_description")]
                and int(row.get("occurrences", 0)) == 1
            }
            return linked == expected
        except (KeyError, TypeError, ValueError):
            return False
