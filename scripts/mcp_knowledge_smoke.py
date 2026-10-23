#!/usr/bin/env python3
"""Exercise the supported MCP evidence-to-graph reader workflow."""

from __future__ import annotations

import argparse
import asyncio
import json
from typing import Any

from harborrag_mcp_server.server import McpServer
from harborrag_runtime.composition.readers import open_reader_application
from harborrag_runtime.config.settings import RuntimeSettings


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant", help="Tenant ID; defaults to runtime ingestion tenant.")
    parser.add_argument("--principal", required=True, help="Authorized principal ID.")
    parser.add_argument("--query", required=True, help="Evidence search question.")
    parser.add_argument("--entity", help="Optional exact graph-node title.")
    parser.add_argument("--target", help="Optional second exact title for a bounded path.")
    parser.add_argument("--compact", action="store_true", help="Print result counts only.")
    return parser.parse_args()


async def _run(arguments: argparse.Namespace) -> dict[str, object]:
    settings = RuntimeSettings()
    tenant = arguments.tenant or settings.ingestion_tenant_id
    runtime = open_reader_application(settings)
    server = McpServer(invoker=runtime.invoker, references=runtime.references)
    identity = {"tenant_id": tenant}
    output: dict[str, object] = {}
    try:
        await runtime.start()
        output["sources"] = await server.call_tool(
            "list_sources", identity, principal_id=arguments.principal
        )
        search = await server.call_tool(
            "vector_search",
            {**identity, "query": arguments.query, "top_k": 5},
            principal_id=arguments.principal,
        )
        output["search"] = search
        selectors = _evidence_selectors(search)
        if selectors:
            output["evidence"] = await server.call_tool(
                "fetch_evidence",
                {**identity, "items": selectors},
                principal_id=arguments.principal,
            )
        start = await _resolve(server, identity, arguments.principal, arguments.entity)
        if start is not None:
            output["start"] = start
            start_key = _unique_node_key(start)
            if start_key is not None:
                output["subgraph"] = await server.call_tool(
                    "graph_subgraph_search",
                    {**identity, "start_node": start_key, "max_depth": 2},
                    principal_id=arguments.principal,
                )
                target = await _resolve(server, identity, arguments.principal, arguments.target)
                if target is not None:
                    output["target"] = target
                    target_key = _unique_node_key(target)
                    if target_key is not None:
                        output["path"] = await server.call_tool(
                            "graph_path_search",
                            {
                                **identity,
                                "start_node": start_key,
                                "end_node": target_key,
                                "max_depth": 4,
                            },
                            principal_id=arguments.principal,
                        )
        return output
    finally:
        await runtime.aclose()


async def _resolve(
    server: McpServer,
    identity: dict[str, str],
    principal_id: str,
    title: str | None,
) -> dict[str, object] | None:
    if not title:
        return None
    return await server.call_tool(
        "resolve_graph_nodes",
        {**identity, "selector": {"kind": "exact_title", "value": title}},
        principal_id=principal_id,
    )


def _evidence_selectors(search: dict[str, object]) -> list[dict[str, str]]:
    results = search.get("results")
    if not isinstance(results, list):
        return []
    selectors = []
    for raw in results[:10]:
        if not isinstance(raw, dict) or not isinstance(raw.get("id"), str):
            continue
        selector = {"chunk_id": raw["id"]}
        metadata = raw.get("metadata")
        if isinstance(metadata, dict):
            _copy_string(metadata, selector, "document_id", "expected_document_id")
            _copy_string(
                metadata,
                selector,
                "document_version_id",
                "expected_document_version_id",
            )
        selectors.append(selector)
    return selectors


def _copy_string(
    source: dict[str, Any], target: dict[str, str], source_key: str, target_key: str
) -> None:
    value = source.get(source_key)
    if isinstance(value, str):
        target[target_key] = value


def _unique_node_key(result: dict[str, object]) -> str | None:
    if result.get("resolution") != "unique":
        return None
    candidates = result.get("candidates")
    if not isinstance(candidates, list) or len(candidates) != 1:
        return None
    candidate = candidates[0]
    value = candidate.get("node_key") if isinstance(candidate, dict) else None
    return value if isinstance(value, str) else None


def _compact(result: dict[str, object]) -> dict[str, object]:
    output: dict[str, object] = {}
    for name, raw in result.items():
        if not isinstance(raw, dict):
            continue
        item: dict[str, object] = {"ok": raw.get("ok")}
        for field in ("results", "items", "sources", "candidates", "nodes", "paths"):
            values = raw.get(field)
            if isinstance(values, list):
                item[f"{field}_count"] = len(values)
        if "resolution" in raw:
            item["resolution"] = raw["resolution"]
        if "outcome" in raw:
            item["outcome"] = raw["outcome"]
        output[name] = item
    return output


def main() -> int:
    arguments = _arguments()
    result = asyncio.run(_run(arguments))
    print(json.dumps(_compact(result) if arguments.compact else result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
