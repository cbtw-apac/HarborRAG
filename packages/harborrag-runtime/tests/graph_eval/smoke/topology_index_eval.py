"""Live semantic-v3 topology indexing and exact-verification experiment.

The experiment writes only tenant-hashed graphs under ``hr_topology_eval_*``. It
verifies structural-node enrichment, entity descriptions, consolidated typed edges,
bottom-up parent summaries, corruption detection, repair and tenant isolation.

Exit codes: 0 all checks pass, 1 a check fails, 2 prerequisites are unavailable.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from graph_eval.smoke import configure_logging  # noqa: E402
from graph_eval.smoke.configuration import build_config  # noqa: E402
from harborrag_adapters.repositories.graph.falkordb.knowledge_support import (  # noqa: E402
    read_rows,
)
from harborrag_adapters.repositories.graph.falkordb.tenant_pool import (  # noqa: E402
    TenantGraphRegistry,
)
from harborrag_adapters.repositories.graph.falkordb.topology import (  # noqa: E402
    FalkorTopologyRepository,
)
from harborrag_core.ingestion import ArtifactReference  # noqa: E402
from harborrag_core.storage import StorageOperationContext  # noqa: E402
from harborrag_core.topology import (  # noqa: E402
    ChunkExtractionInput,
    DocumentTopologyBuild,
    EvidenceSpan,
    ExtractedAssertion,
    ExtractedEntity,
    ExtractionOutput,
    ExtractionProfile,
    TopologyJob,
    TopologyPolicy,
)
from harborrag_core.topology.derived import DescriptionOutput  # noqa: E402
from harborrag_engine.topology.assembly import assemble_observations  # noqa: E402
from harborrag_engine.topology.build_builder import EnrichmentBuildBuilder  # noqa: E402
from harborrag_engine.topology.parent_builder import ParentDescriptionBuilder  # noqa: E402

logger = logging.getLogger("harborrag.graph_eval.topology_index_eval")
TENANT = "topology-eval-tenant-a"
OTHER_TENANT = "topology-eval-tenant-b"
PREFIX = "hr_topology_eval"


@dataclass(frozen=True)
class TopologyExperimentFactory:
    """Build a grounded semantic-v3 fixture with repeated assertion support."""

    tenant_id: str = TENANT

    def build(self) -> DocumentTopologyBuild:
        inputs = tuple(
            ChunkExtractionInput(
                chunk_id=f"chunk-{index}",
                content="Alpha depends on Beta.",
                source_title="Service dependency guide",
                heading_path=("Architecture",),
            )
            for index in (1, 2)
        )
        outputs = {value.chunk_id: self._output(value) for value in inputs}
        job = self._job()
        observations, _ = assemble_observations(job, outputs)
        resolved = {
            item.entity_id: f"canonical-{item.observation.local_id}" for item in observations
        }
        content = EnrichmentBuildBuilder(job).build(inputs, outputs, resolved=resolved)
        payload = content.model_dump_json().encode()
        artifact = ArtifactReference(
            bucket="graph-eval",
            key=f"topology/{content.build_id}.json",
            sha256=sha256(payload).hexdigest(),
            byte_size=len(payload),
            media_type="application/json",
        )
        return DocumentTopologyBuild(**content.model_dump(), artifact=artifact)

    @staticmethod
    def _output(value: ChunkExtractionInput) -> ExtractionOutput:
        entities = tuple(
            ExtractedEntity(
                local_id=local_id,
                name=name,
                entity_type="service",
                description=f"{name} is a service in the dependency guide.",
                description_evidence=(EvidenceSpan(start=start, end=end, quote=name),),
                span=EvidenceSpan(start=start, end=end, quote=name),
            )
            for local_id, name, start, end in (
                ("alpha", "Alpha", 0, 5),
                ("beta", "Beta", 17, 21),
            )
        )
        return ExtractionOutput(
            entities=entities,
            assertions=(
                ExtractedAssertion(
                    local_id="dependency",
                    subject_id="alpha",
                    object_id="beta",
                    predicate="depends_on",
                    statement_text=value.content,
                    span=EvidenceSpan(start=0, end=22, quote=value.content),
                ),
            ),
            title="Service dependency",
            description=value.content,
            retrieval_context=value.content,
            title_evidence=(EvidenceSpan(start=0, end=7, quote="Service", source="source_title"),),
            description_evidence=(EvidenceSpan(start=0, end=22, quote=value.content),),
            retrieval_context_evidence=(EvidenceSpan(start=0, end=22, quote=value.content),),
        )

    def _job(self) -> TopologyJob:
        profile = ExtractionProfile(
            model="deterministic-eval",
            deployment_revision="fixture-v1",
            prompt_digest="topology-eval-v1",
            schema_version="3",
            ontology_version="builtin-enterprise-v1",
        )
        return TopologyJob(
            job_id="topology-index-eval-job",
            tenant_id=self.tenant_id,
            source_scope_id="topology-eval-source",
            document_id="topology-eval-document",
            document_version_id="topology-eval-version",
            policy=TopologyPolicy(
                tenant_id=self.tenant_id,
                source_scope_id="topology-eval-source",
                enabled=True,
                profile=profile,
            ),
            policy_revision=1,
            state="running",
        )


class SummaryGenerator:
    async def generate_usage(self, packets):  # type: ignore[no-untyped-def]
        from harborrag_adapters.topology.descriptions import DescriptionRun
        from harborrag_core.models.chat import HarborChatUsage

        return DescriptionRun(await self.generate(packets), HarborChatUsage(), 1)

    async def generate(self, packets):  # type: ignore[no-untyped-def]
        return DescriptionOutput(
            description="Alpha depends on Beta in the architecture.",
            cited_packet_ids=tuple(packet.packet_id for packet in packets),
            complete=True,
        )


async def _seed_structure(
    repository: FalkorTopologyRepository,
    build: DocumentTopologyBuild,
    context: StorageOperationContext,
) -> None:
    database = await repository.database_for(context, write=True)
    parameters = {
        "tenant_id": str(context.tenant_id),
        "document_id": build.document_id,
        "document_version_id": build.document_version_id,
    }
    await database.write(
        "UNWIND $chunk_ids AS chunk_id CREATE (:KnowledgeNode:Chunk {tenant_id: $tenant_id, "
        "node_key: chunk_id, document_id: $document_id, "
        "document_version_id: $document_version_id, title: 'structural'})",
        {**parameters, "chunk_ids": list(build.chunk_ids)},
    )
    await database.write(
        "CREATE (:KnowledgeNode:DocumentVersion {tenant_id: $tenant_id, node_key: 'document', "
        "document_id: $document_id, document_version_id: $document_version_id}), "
        "(:KnowledgeNode:Structure {tenant_id: $tenant_id, node_key: 'section', "
        "entity_type: 'section', section_path: ['Architecture'], "
        "document_id: $document_id, document_version_id: $document_version_id})",
        parameters,
    )


async def _cleanup_structure(
    repository: FalkorTopologyRepository,
    build: DocumentTopologyBuild,
    context: StorageOperationContext,
) -> None:
    database = await repository.database_for(context, write=True)
    await database.write(
        "MATCH (n:KnowledgeNode {tenant_id: $tenant_id, "
        "document_version_id: $document_version_id}) DETACH DELETE n",
        {
            "tenant_id": str(context.tenant_id),
            "document_version_id": build.document_version_id,
        },
    )


async def _counts(
    repository: FalkorTopologyRepository,
    build: DocumentTopologyBuild,
    context: StorageOperationContext,
) -> tuple[int, int]:
    database = await repository.database_for(context)
    parameters = {"tenant_id": str(context.tenant_id), "build_id": build.build_id}
    node_rows = await read_rows(
        database,
        "MATCH (n:TopologyRecord {tenant_id: $tenant_id, build_id: $build_id}) "
        "RETURN count(n) AS count",
        parameters,
    )
    edge_rows = await read_rows(
        database,
        "MATCH ()-[r {tenant_id: $tenant_id, build_id: $build_id}]->() RETURN count(r) AS count",
        parameters,
    )
    return int(node_rows[0]["count"]), int(edge_rows[0]["count"])


async def _quality(
    repository: FalkorTopologyRepository,
    build: DocumentTopologyBuild,
    context: StorageOperationContext,
) -> dict[str, int]:
    database = await repository.database_for(context)
    parameters = {"tenant_id": str(context.tenant_id), "build_id": build.build_id}
    statements = {
        "described_nodes": "MATCH (n:KnowledgeNode {tenant_id: $tenant_id, "
        "description_build_id: $build_id}) RETURN count(n) AS count",
        "described_entities": "MATCH (n:TopologyRecord {tenant_id: $tenant_id, "
        "build_id: $build_id}) WHERE n.description IS NOT NULL RETURN count(n) AS count",
        "parent_summaries": "MATCH (n:TopologyParentView {tenant_id: $tenant_id, "
        "build_id: $build_id}) RETURN count(n) AS count",
        "parallel_edges": "MATCH (s)-[r {tenant_id: $tenant_id, build_id: $build_id}]->(t) "
        "WITH s, t, type(r) AS kind, count(r) AS occurrences WHERE occurrences > 1 "
        "RETURN count(kind) AS count",
    }
    return {
        name: int((await read_rows(database, statement, parameters))[0]["count"])
        for name, statement in statements.items()
    }


async def run() -> int:
    config = build_config().model_copy(
        update={
            "tenant_isolation": True,
            "tenant_graph_prefix": PREFIX,
            "max_cached_tenants": 4,
        }
    )
    repository = FalkorTopologyRepository(config)
    context = StorageOperationContext.system(TENANT, operation_kind="topology-index-eval")
    other = StorageOperationContext.system(OTHER_TENANT, operation_kind="topology-index-eval")
    build = TopologyExperimentFactory().build()
    parents = await ParentDescriptionBuilder(SummaryGenerator()).build(
        build.document_id, build.representations
    )
    connected = False
    try:
        await repository.connect(provision=False)
        connected = True
        await repository.delete_build(build.build_id, context=context)
        await _cleanup_structure(repository, build, context)
        await _seed_structure(repository, build, context)
        await repository.write(build, context=context)
        await repository.write_parents(build, parents, context=context)
        initial = await repository.verify(build, context=context)
        parents_verified = await repository.verify_parents(build, parents, context=context)
        node_count, edge_count = await _counts(repository, build, context)
        quality = await _quality(repository, build, context)
        database = await repository.database_for(context, write=True)
        await database.write(
            "MATCH (n:KnowledgeNode:Chunk {tenant_id: $tenant_id, "
            "description_build_id: $build_id, node_key: $record_key}) "
            "SET n.description = 'corrupt'",
            {
                "tenant_id": TENANT,
                "build_id": build.build_id,
                "record_key": "chunk-1",
            },
        )
        corruption_detected = not await repository.verify(build, context=context)
        await repository.delete_build(build.build_id, context=context)
        retained = await read_rows(
            database,
            "MATCH (n:KnowledgeNode:Chunk {tenant_id: $tenant_id, title: 'structural'}) "
            "RETURN count(n) AS count",
            {"tenant_id": TENANT},
        )
        await repository.write(build, context=context)
        await repository.write_parents(build, parents, context=context)
        repair_verified = await repository.verify(
            build, context=context
        ) and await repository.verify_parents(build, parents, context=context)
        await repository.database_for(other, write=True)
        foreign_nodes, foreign_edges = await _counts(repository, build, other)
        registry = TenantGraphRegistry(PREFIX)
        tenant_isolated = registry.graph_for(context) != registry.graph_for(other)
        checks = {
            "initial_verify": initial,
            "parent_verify": parents_verified,
            "expected_shape": (node_count, edge_count) == (2, 7),
            "node_descriptions": quality["described_nodes"] == 4,
            "entity_descriptions": quality["described_entities"] == 2,
            "higher_summaries": quality["parent_summaries"] == 2,
            "unique_typed_edges": quality["parallel_edges"] == 0,
            "corruption_detected": corruption_detected,
            "structural_titles_preserved": int(retained[0]["count"]) == 2,
            "repair_verified": repair_verified,
            "tenant_graphs_distinct": tenant_isolated,
            "foreign_graph_empty": (foreign_nodes, foreign_edges) == (0, 0),
        }
        print(
            json.dumps(
                {"checks": checks, "topology_nodes": node_count, "edges": edge_count, **quality}
            )
        )
        return 0 if all(checks.values()) else 1
    except Exception as error:  # noqa: BLE001 - prerequisite probe
        logger.error("prerequisites unavailable: %s", error)
        return 2
    finally:
        if connected:
            try:
                await repository.delete_build(build.build_id, context=context)
            finally:
                await _cleanup_structure(repository, build, context)
        await repository.close()


if __name__ == "__main__":
    argparse.ArgumentParser(description=__doc__).parse_args()
    configure_logging()
    raise SystemExit(asyncio.run(run()))
