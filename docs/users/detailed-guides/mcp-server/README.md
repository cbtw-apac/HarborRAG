# MCP Tools

`harborrag-mcp-server` exposes HarborRAG retrieval to MCP clients - IDEs, agents, and
anything else that speaks the Model Context Protocol - through an audited,
policy-bounded FastMCP transport.

**Want to get it running first?** Jump to
[Setup and Integration](setup-and-integration.md). This page describes what the tools do.

| Tool | Arguments | Result |
| --- | --- | --- |
| `vector_search` | Query, tenant, top-k, lane, filters, `observe_graph`, threshold | Vector results and diagnostics |
| `fetch_evidence` | Tenant and up to 10 chunk IDs with optional expected document/version | Reauthorized immutable artifact text and per-item availability |
| `get_document_context` | Tenant, document, optional anchor/version/cursor, limit | Ordered version-bound chunks, bounded outline, continuation cursor |
| `list_sources` | Tenant, optional source/type filters, cursor, limit | Readable corpus scopes and safe freshness metadata |
| `describe_graph` | Empty object | Static schema versions, property catalogs, selectors, topologies, and workflows |
| `graph_triplet_search` | Tenant plus subject, predicate, or object | Active canonical triplets |
| `graph_subgraph_search` | Tenant, start node, depth and direction | Active bounded nodes and relations |
| `graph_path_search` | Tenant, start/end nodes, depth and direction | Active bounded paths |
| `resolve_graph_nodes` | Tenant, exact typed selector, optional source/type scope | Authorized candidates with explicit ambiguity |

Call `describe_graph` first — before any other graph tool — if you are not yet
familiar with the graph model, or if graph selectors, relations, or directions are
unclear. It is a static schema lookup, not a query. The MCP server also advertises
short cross-tool routing instructions (which tool to call for which intent) to any
client that surfaces server-level `instructions`.

The catalog contains exactly nine read-only tools. Eight require an explicit `tenant_id`;
`describe_graph` accepts `{}` because it returns only the static supported model.

| Tool | Required arguments | Optional arguments | Returns |
| --- | --- | --- | --- |
| `vector_search` | `query`, `tenant_id` | `top_k` (1–20, default 5), `lane` (`dense`/`sparse`/`hybrid`, default `hybrid`), `filters`, `observe_graph`, `score_threshold` (0.0–1.0) | Vector results and retrieval diagnostics |
| `fetch_evidence` | `tenant_id`, `items[].chunk_id` | Expected document/version IDs | Canonical text, citation locator, and `available`/`unavailable`/`output_limit` per item |
| `get_document_context` | `tenant_id`, `document_id` | Expected version, one anchor, cursor, limit (1–10), outline | Ordered chunks and a tenant/principal/version-bound cursor |
| `list_sources` | `tenant_id` | Source IDs, connector types, cursor, limit (1–20) | Only readable source scopes; never connection configuration |
| `describe_graph` | None | None | Static full graph catalog |
| `graph_triplet_search` | `tenant_id`, plus at least one of `subject`, `predicate`, `object` | `limit` (1–20, default 10) | Active canonical subject–predicate–object records |
| `graph_subgraph_search` | `tenant_id`, `start_node` | Relation types, depth (1–4), nodes (1–20), direction | ACL-filtered connected neighborhood, at most 40 edges, with completion reasons |
| `graph_path_search` | `tenant_id`, `start_node`, `end_node` | Relation types, depth (1–4), paths (1–5), direction | ACL-filtered bounded paths with stable relation provenance |
| `resolve_graph_nodes` | `tenant_id`, selector kind/value | Source IDs, entity types, limit (1–10) | Unique, ambiguous, or no-match candidates |

Chat and agent are **not** MCP tools. They are served only through the HarborRAG REST API
at `/v1/chat/completions` and `/v1/agent/completions` - see [Chat](../../chat/README.md).
Ingestion is controlled through the CLI or the authenticated API, never through MCP.

> Defaults advertised in the schema can be overridden per deployment in
> [`config/mcp.yaml`](../../../../config/mcp.yaml). The checked-in file sets
> `observe_graph: true` for `vector_search`, which is the opposite of the schema default,
> so confirm the effective value with `GET /api/tools?tenant_id=<tenant>` rather than
> assuming the schema default applies.

## Evidence and graph workflow

Use `list_sources` when the corpus is unclear, `vector_search` to discover evidence, and
`fetch_evidence` before citing selected chunk IDs. Use `get_document_context` when ordered
surrounding text is needed. The graph tools navigate; they do not replace source evidence.

Graph selectors support three exact forms:

- a `node_key`
- a `logical_id`
- an exact, complete `title` - matched case-insensitively, never partially, and unset on
  chunk nodes

In practice use a `chunk_id` from `vector_search` (also its Chunk node key), or call
`resolve_graph_nodes` and pass a returned stable `node_key`. Duplicate titles remain
explicitly ambiguous.

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

### `observe_graph` is diagnostics, not evidence

`vector_search(observe_graph=true)` adds a *shallow provenance observation*: it seeds up
to ten of the returned `chunk_id`s, traverses two hops in both directions, and summarizes
the counts, documents, and sections it touched under the response's `diagnostics` field.
It never loads the content of any newly discovered chunk, never ranks neighboring
evidence, and a graph failure silently degrades to an empty observation rather than
failing the vector call. Treat it as provenance context for the results you already have
— not as a way to retrieve additional evidence for a generator. Retrieving and ranking
neighboring evidence is a separate, not-yet-available composed operation.

## Policy and audit status

`McpServer` records every call attempt and outcome, validates each declared
JSON schema, and enforces argument, result-count, and output-size budgets.
Every tool call requires an explicit tenant (except `describe_graph` static tool). Retrieval propagates the caller's
principal through the runtime access context. MCP audits store argument
digests rather than raw query text.

1. **Capability check** - all nine tools declare `read`; nothing else is registered.
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
