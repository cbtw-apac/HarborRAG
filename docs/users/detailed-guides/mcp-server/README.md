# MCP Tools

`harborrag-mcp-server` exposes HarborRAG retrieval to MCP clients - IDEs, agents, and
anything else that speaks the Model Context Protocol - through an audited,
policy-bounded FastMCP transport.

**Want to get it running first?** Jump to
[Setup and Integration](setup-and-integration.md). This page describes what the tools do.

## The four tools

The catalog contains exactly four tools. All of them are read-only, and every one requires
an explicit `tenant_id`.

| Tool | Required arguments | Optional arguments | Returns |
| --- | --- | --- | --- |
| `vector_search` | `query`, `tenant_id` | `top_k` (1–20, default 5), `lane` (`dense`/`sparse`/`hybrid`, default `hybrid`), `filters`, `observe_graph`, `score_threshold` (0.0–1.0) | Vector results and retrieval diagnostics |
| `graph_triplet_search` | `tenant_id`, plus at least one of `subject`, `predicate`, `object` | `limit` (1–20, default 10) | Active canonical subject–predicate–object records |
| `graph_path_search` | `tenant_id`, `start_node`, `end_node` | `relationship_types`, `max_depth` (1–8, default 4), `max_paths` (1–20, default 10), `direction` (`incoming`/`outgoing`/`both`, default `both`) | Active bounded paths between the two nodes |
| `graph_subgraph_search` | `tenant_id`, `start_node` | `relationship_types`, `max_depth` (1–8, default 2), `max_nodes` (1–20, default 20), `direction` (default `both`) | Active bounded neighborhood of nodes and relations |

Chat and agent are **not** MCP tools. They are served only through the HarborRAG REST API
at `/v1/chat/completions` and `/v1/agent/completions` - see [Chat](../../chat/README.md).
Ingestion is controlled through the CLI or the authenticated API, never through MCP.

> Defaults advertised in the schema can be overridden per deployment in
> [`config/mcp.yaml`](../../../../config/mcp.yaml). The checked-in file sets
> `observe_graph: true` for `vector_search`, which is the opposite of the schema default,
> so confirm the effective value with `GET /api/tools?tenant_id=<tenant>` rather than
> assuming the schema default applies.

## Start with `vector_search`

`vector_search` is the entry point, because the three graph tools need a node selector you
must already hold. Only three things resolve to a node:

- a `node_key`
- a `logical_id`
- an exact, complete `title` - matched case-insensitively, never partially, and unset on
  chunk nodes

In practice the selector you use is a `chunk_id` from a `vector_search` result: chunk IDs
and `Chunk` node keys are the same value. So the usual sequence is *search, then expand*.

`graph_triplet_search` is the one exception - it is satisfiable by `predicate` alone, which
is a relation-type enum rather than a node selector, so you can enumerate relationships of
a given type without holding a node.

## Calling the tools in Python

Two surfaces exist, and they return different shapes. Pick one and stay with it:

```python
# Module-level convenience: returns a list of plain dicts.
from harborrag_mcp_server.server import list_tools

print(list_tools())
```

```python
# Server instance: returns McpToolSpec objects with .name and .input_schema.
from harborrag_mcp_server.server import McpServer
from harborrag_runtime.sdk import HarborRAG, HarborRAGConfig

server = McpServer(runtime=HarborRAG(HarborRAGConfig()))
for spec in server.list_tools():
    print(spec.name, spec.input_schema)

result = await server.call_tool(
    "vector_search",
    {"query": "publication policy", "tenant_id": "default"},
)
```

An unknown tool name raises `ValueError`.

## Policy and audit

Every call passes the same boundary, whether it arrives over stdio, over HTTP, or from the
browser Tool Playground:

1. **Capability check** - all four tools declare `read`; nothing else is registered.
2. **Schema validation** - the declared JSON schema, with `additionalProperties: false`.
3. **Argument budget** - a serialized-argument size ceiling.
4. **Tenant scope** - `tenant_id` is required, and `filters` explicitly cannot carry a
   `tenant_id` to smuggle a different scope past the check.
5. **Execution** - retrieval propagates the caller's principal through the runtime access
   context.
6. **Result budgets** - result-count and serialized-output ceilings.
7. **Audit** - an owner-only JSONL record of the principal, an arguments *digest*, and the
   outcome. Raw query text and bearer tokens are never written.

Network transports must supply a FastMCP authentication provider. The only exception is
local unauthenticated stdio, which the caller has to select explicitly and which opens no
listener.

## Next

- [Setup and Integration](setup-and-integration.md) - clients, the local HTTP UI, bearer
  tokens, tool configuration, and the container image
- [Chat](../../chat/README.md) - the chat contract and model setup
- [Troubleshooting](../../troubleshooting/README.md) - when a client cannot connect
