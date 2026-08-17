"""Whole-graph conformance and structural-health gate over live FalkorDB.

Usage:
    .venv/bin/python packages/harborrag-runtime/tests/graph_eval/smoke/graph_health.py \
        [--tenant TENANT_ID ...] [--output report.json] [--identities]

`--identities` adds sorted `node_keys` and `relation_ids` to every report, the
baseline material `graph_diff.py` needs; they are omitted entirely without it.

Exit codes: 0 all gates pass, 1 gate failure, 2 prerequisites unavailable.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

# Standalone-script mode: make the tests/ root importable so the shared library
# (graph_eval.corpus, .golden, .health) resolves the same way it does under pytest.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from graph_eval.health.metrics import (  # noqa: E402
    GraphHealthReport,
    compute_report,
    connected_component_sizes,
)
from graph_eval.smoke.configuration import build_client  # noqa: E402
from harborrag_adapters.repositories.graph.falkordb.client import FalkorDBClient  # noqa: E402
from harborrag_adapters.repositories.graph.falkordb.knowledge_support import (  # noqa: E402
    read_rows,
)
from harborrag_core.ingestion import GRAPH_SCHEMA_VERSION  # noqa: E402

_TENANTS = """
MATCH (node:KnowledgeNode)
WHERE node.graph_schema_version = $graph_schema_version
RETURN DISTINCT node.tenant_id AS tenant_id
"""
_NODE_CENSUS = """
MATCH (node:KnowledgeNode)
WHERE node.tenant_id = $tenant_id AND node.graph_schema_version = $graph_schema_version
RETURN node.node_kind AS kind, node.entity_type AS entity_type, count(node) AS item_count
"""
_RELATION_CENSUS = """
MATCH (source:KnowledgeNode)-[relation]->(target:KnowledgeNode)
WHERE relation.tenant_id = $tenant_id
  AND relation.graph_schema_version = $graph_schema_version
RETURN source.node_kind AS source_kind, relation.relation_type AS relation_type,
       target.node_kind AS target_kind, count(relation) AS item_count
"""
_ORPHAN_CENSUS = """
MATCH (node:KnowledgeNode)
WHERE node.tenant_id = $tenant_id AND node.graph_schema_version = $graph_schema_version
  AND NOT (node)--()
RETURN node.node_kind AS kind, count(node) AS item_count
"""
_DUPLICATES = """
MATCH (source:KnowledgeNode)-[relation]->(target:KnowledgeNode)
WHERE relation.tenant_id = $tenant_id
  AND relation.graph_schema_version = $graph_schema_version
WITH relation.relation_type AS relation_type, source.node_key AS source_key,
     target.node_key AS target_key, count(relation) AS occurrences
WHERE occurrences > 1
RETURN count(relation_type) AS item_count
"""
# ponytail: substring match works because attribute encoding is canonical JSON
# (sorted keys, compact separators); switch to client-side decode if that changes.
_PLACEHOLDERS = """
MATCH (node:KnowledgeNode:SourceEntity)
WHERE node.tenant_id = $tenant_id AND node.graph_schema_version = $graph_schema_version
  AND node.attributes CONTAINS '"placeholder":true'
RETURN count(node) AS item_count
"""
_HUBS = """
MATCH (node:KnowledgeNode)-[relation]-()
WHERE node.tenant_id = $tenant_id AND node.graph_schema_version = $graph_schema_version
RETURN node.node_key AS node_key, node.node_kind AS kind, node.title AS title,
       count(relation) AS degree
ORDER BY degree DESC
LIMIT 10
"""
_NODE_KEYS = """
MATCH (node:KnowledgeNode)
WHERE node.tenant_id = $tenant_id AND node.graph_schema_version = $graph_schema_version
RETURN node.node_key AS identity ORDER BY identity
"""
# ponytail: full identity lists in the JSON -- fine at smoke scale; move to
# per-document digests if reports outgrow review.
_RELATION_IDS = """
MATCH (:KnowledgeNode)-[relation]->(:KnowledgeNode)
WHERE relation.tenant_id = $tenant_id
  AND relation.graph_schema_version = $graph_schema_version
RETURN relation.relation_id AS identity ORDER BY identity
"""
_EDGE_ENDPOINTS = """
MATCH (source:KnowledgeNode)-[relation]->(target:KnowledgeNode)
WHERE relation.tenant_id = $tenant_id
  AND relation.graph_schema_version = $graph_schema_version
RETURN source.node_key AS source_key, target.node_key AS target_key
"""
# Graph-level, not per-tenant: a FAILED constraint is NOT enforced by FalkorDB, and a
# node missing a merge-identity property is invisible to every tenant-scoped census.
# Observed columns on FalkorDB v4.20.1: type, label, properties, entitytype, status.
_CONSTRAINTS = "CALL db.constraints()"
_UNATTRIBUTED = """
MATCH (node:KnowledgeNode)
WHERE node.node_key IS NULL OR node.tenant_id IS NULL
   OR node.graph_schema_version IS NULL
RETURN count(node) AS item_count
"""


async def _single_count(
    client: FalkorDBClient, statement: str, parameters: Mapping[str, Any]
) -> int:
    rows = await read_rows(client, statement, parameters)
    return int(rows[0]["item_count"]) if rows else 0


async def _tenant_report(
    client: FalkorDBClient, tenant_id: str
) -> tuple[GraphHealthReport, list[str]]:
    parameters = {"tenant_id": tenant_id, "graph_schema_version": GRAPH_SCHEMA_VERSION}
    node_keys = [str(row["identity"]) for row in await read_rows(client, _NODE_KEYS, parameters)]
    edges = [
        (str(row["source_key"]), str(row["target_key"]))
        for row in await read_rows(client, _EDGE_ENDPOINTS, parameters)
    ]
    report = compute_report(
        tenant_id=tenant_id,
        node_census=await read_rows(client, _NODE_CENSUS, parameters),
        relation_census=await read_rows(client, _RELATION_CENSUS, parameters),
        orphan_census=await read_rows(client, _ORPHAN_CENSUS, parameters),
        placeholder_count=await _single_count(client, _PLACEHOLDERS, parameters),
        duplicate_semantic_count=await _single_count(client, _DUPLICATES, parameters),
        top_hubs=await read_rows(client, _HUBS, parameters),
        component_sizes=connected_component_sizes(node_keys, edges),
    )
    return report, node_keys


async def _graph_level_failures(client: FalkorDBClient) -> list[str]:
    """Failures that no tenant-scoped census can see (see _CONSTRAINTS/_UNATTRIBUTED)."""
    failures: list[str] = []
    for row in await read_rows(client, _CONSTRAINTS, {}):
        if str(row.get("status", "")).upper() == "FAILED":
            failures.append(f"graph: constraint FAILED (unenforced): {dict(row)}")
    unattributed = await _single_count(client, _UNATTRIBUTED, {})
    if unattributed:
        failures.append(f"graph: nodes missing merge-identity properties: {unattributed}")
    return failures


async def run(tenants: list[str], output: Path | None, *, identities: bool) -> int:
    try:
        client = build_client()
        await client.connect()
    except Exception as error:  # noqa: BLE001 - prerequisite probe
        print(f"prerequisites unavailable: {error}", file=sys.stderr)
        return 2
    failures: list[str] = []
    payload: list[dict[str, object]] = []
    try:
        failures.extend(await _graph_level_failures(client))
        if not tenants:
            rows = await read_rows(client, _TENANTS, {"graph_schema_version": GRAPH_SCHEMA_VERSION})
            tenants = sorted(str(row["tenant_id"]) for row in rows)
            # Discovery finding nothing is itself the failure: a never-populated graph,
            # or one left behind at an older version after a schema bump, would
            # otherwise report green by having no tenant to run the empty gate against.
            if not tenants:
                failures.append(
                    f"graph: no tenants found at graph_schema_version {GRAPH_SCHEMA_VERSION}"
                )
        for tenant in tenants:
            report, node_keys = await _tenant_report(client, tenant)
            entry = report.as_dict()
            # Keys are absent rather than null without the flag: diff_reports disables
            # its Jaccard gates on a missing key but would choke on a null one.
            if identities:
                parameters = {"tenant_id": tenant, "graph_schema_version": GRAPH_SCHEMA_VERSION}
                entry["node_keys"] = node_keys
                entry["relation_ids"] = [
                    str(row["identity"])
                    for row in await read_rows(client, _RELATION_IDS, parameters)
                ]
            payload.append(entry)
            failures.extend(f"{report.tenant_id}: {failure}" for failure in report.gate_failures)
    # Gate failures are only ever collected into lists, never raised, so anything
    # escaping the block above is infrastructural, not a graph verdict.
    except Exception as error:  # noqa: BLE001 - prerequisite probe
        print(f"prerequisites unavailable: {error}", file=sys.stderr)
        return 2
    finally:
        await client.close()
    print(json.dumps(payload, indent=2, sort_keys=True))
    if output is not None:
        output.write_text(json.dumps(payload, indent=2, sort_keys=True))
    for failure in failures:
        print(f"GATE FAILURE {failure}", file=sys.stderr)
    return 1 if failures else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant", action="append", default=[], dest="tenants")
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--identities", action="store_true")
    arguments = parser.parse_args()
    return asyncio.run(run(arguments.tenants, arguments.output, identities=arguments.identities))


if __name__ == "__main__":
    raise SystemExit(main())
