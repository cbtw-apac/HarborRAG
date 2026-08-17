"""Mutation check: seeded violations must trip the live health gates (VIO pattern).

CI verifies the gate logic on synthetic census rows; this script verifies the
Cypher censuses themselves. It seeds the eval corpus into the isolated
'harborrag-graph-eval' graph key, injects one violation per gated census, and
asserts every gate fires. Exit: 0 all fired, 1 a gate missed, 2 unavailable.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

# Standalone-script mode: make the tests/ root importable so the shared library
# (graph_eval.corpus, .golden, .health) resolves the same way it does under pytest.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from graph_eval.corpus import GRAPH_NAME, TENANT_ID, build_corpus  # noqa: E402
from graph_eval.smoke.configuration import build_client  # noqa: E402
from graph_eval.smoke.graph_health import _tenant_report  # noqa: E402
from harborrag_adapters.repositories.graph.falkordb import (  # noqa: E402
    FalkorDBGraphConfig,
    FalkorKnowledgeGraphRepository,
)
from harborrag_core.ingestion import GRAPH_SCHEMA_VERSION  # noqa: E402
from harborrag_core.schemas.storage import StorageOperationContext  # noqa: E402

# (name, violation Cypher, substring the resulting gate failure must contain) —
# one entry per gated census in metrics.py. Every mutation must carry tenant_id and
# graph_schema_version: the censuses filter on both, so a record missing either is
# invisible to the gate it is meant to trip (and to _SWEEP's contract counterpart).
_MUTATIONS: tuple[tuple[str, str, str], ...] = (
    (
        "unknown relation type",
        """
        MATCH (a:KnowledgeNode {node_key: $runbook, tenant_id: $tenant_id}),
              (b:KnowledgeNode {node_key: $incident, tenant_id: $tenant_id})
        CREATE (a)-[:mystery_edge {relation_type: 'mystery_edge',
            relation_id: 'mutation-edge', tenant_id: $tenant_id,
            graph_schema_version: $graph_schema_version}]->(b)
        """,
        "mystery_edge",
    ),
    (
        "unknown node kind",
        """
        CREATE (:KnowledgeNode:Blob {node_key: 'mutation-blob', node_kind: 'Blob',
            entity_type: 'blob', tenant_id: $tenant_id,
            graph_schema_version: $graph_schema_version, attributes: '{}'})
        """,
        "node kind outside schema: Blob",
    ),
    (
        "orphan version-owned node",
        """
        CREATE (:KnowledgeNode:Chunk {node_key: 'mutation-orphan-chunk',
            node_kind: 'Chunk', entity_type: 'chunk', tenant_id: $tenant_id,
            graph_schema_version: $graph_schema_version, attributes: '{}'})
        """,
        "orphan version-owned nodes: Chunk",
    ),
    (
        "duplicate semantic relation",
        """
        MATCH (a:KnowledgeNode {node_key: $decisions, tenant_id: $tenant_id}),
              (b:KnowledgeNode {node_key: $incident, tenant_id: $tenant_id})
        CREATE (a)-[:links_to {relation_type: 'links_to',
                   relation_id: 'mutation-dup-1', tenant_id: $tenant_id,
                   graph_schema_version: $graph_schema_version}]->(b),
               (a)-[:links_to {relation_type: 'links_to',
                   relation_id: 'mutation-dup-2', tenant_id: $tenant_id,
                   graph_schema_version: $graph_schema_version}]->(b)
        """,
        "duplicate semantic relations",
    ),
)
# Cleanup is raw rather than delete_tenant_projection because the injected records are
# hand-written Cypher outside the projection contract, while that call scopes to
# tenant_id AND graph_schema_version: one mutation that ever omits the version property
# would survive it and poison the eval key for every other script sharing it. Label,
# not property, match on purpose — a mutation node may carry any second label
# (:KnowledgeNode:Blob), and DETACH takes its relations with it.
_SWEEP = """
MATCH (node:KnowledgeNode {tenant_id: $tenant_id})
DETACH DELETE node
"""


async def run() -> int:
    corpus = build_corpus()
    try:
        client = build_client(GRAPH_NAME)
        await client.connect()
    except Exception as error:  # noqa: BLE001 - prerequisite probe
        print(f"prerequisites unavailable: {error}", file=sys.stderr)
        return 2
    context = StorageOperationContext.system(TENANT_ID, operation_kind="graph-eval")
    # The explicit client= wins, so the config's connection fields are inert; they
    # still have to validate, hence plausible loopback values.
    repository = FalkorKnowledgeGraphRepository(
        FalkorDBGraphConfig(host="127.0.0.1", port=6379, graph_name=GRAPH_NAME),
        client=client,
    )
    parameters = {
        "runbook": corpus.source_item_key("runbook"),
        "incident": corpus.source_item_key("incident"),
        "decisions": corpus.source_item_key("decisions"),
        "tenant_id": TENANT_ID,
        "graph_schema_version": GRAPH_SCHEMA_VERSION,
    }
    try:
        try:
            # provision() writes (CREATE INDEX), which is what brings a never-used graph
            # key into existence, and the tenant delete is write-only Cypher, so a first
            # run on a fresh key reaches no read before both have run. Delete-then-write
            # makes reruns idempotent.
            await repository.provision()
            await repository.delete_tenant_projection(context=context)
            for batch in corpus.batches.values():
                await repository.write_projection(batch.nodes, batch.relations, context=context)
            for name, statement, _ in _MUTATIONS:
                result = await client.write(statement, parameters)
                # A MATCH-anchored mutation whose endpoints moved matches nothing and
                # writes nothing, which would read as "GATE DID NOT FIRE" and send the
                # reader to debug a census that is working. Separate the two failures.
                if not (result.nodes_created or result.relationships_created):
                    raise AssertionError(f"mutation wrote nothing (corpus keys changed?): {name}")
            report, _ = await _tenant_report(client, TENANT_ID)
            missed = [
                name
                for name, _, expected in _MUTATIONS
                if not any(expected in failure for failure in report.gate_failures)
            ]
        finally:
            # Unconditional, whatever failed above: retrieval_eval.py and graph_diff.py
            # baselines share this graph key, and a surviving violation would fail every
            # later run of the suite instead of this one.
            await client.write(_SWEEP, {"tenant_id": TENANT_ID})
    # Missed gates are only ever collected into `missed`, never raised, so anything
    # escaping the block above is infrastructural, not a mutation verdict -- including a
    # failed sweep, which leaves residue that must be loud rather than reported as green.
    except Exception as error:  # noqa: BLE001 - prerequisite probe
        print(f"prerequisites unavailable: {error}", file=sys.stderr)
        return 2
    finally:
        await client.close()

    print(f"caught {len(_MUTATIONS) - len(missed)}/{len(_MUTATIONS)} seeded violations")
    for name in missed:
        print(f"GATE DID NOT FIRE: {name}", file=sys.stderr)
    return 1 if missed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
