# MCP Tools

`harborrag-mcp-server` provides an audited, policy-bounded FastMCP transport.

## Available tools

| Tool | Arguments | Result |
| --- | --- | --- |
| `vector_search` | Query, tenant, top-k, lane, filters, `observe_graph`, threshold | Vector results and diagnostics |
| `graph_triplet_search` | Tenant plus subject, predicate, or object | Active canonical triplets |
| `graph_path_search` | Tenant, start/end nodes, depth and direction | Active bounded paths |
| `graph_subgraph_search` | Tenant, start node, depth and direction | Active bounded nodes and relations |
| `describe_graph` | None (no tenant required) | Static graph schema: node kinds, entity types, projected relations, selector rules, connector topologies, recommended workflows |

Call `describe_graph` first if graph selectors, relations, directions, or connector
topology are unclear — it is a static schema lookup, not a query. The MCP server also
advertises short cross-tool routing instructions (which tool to call for which intent)
to any client that surfaces server-level `instructions`.

Chat and agent are not exposed as MCP tools. They are served only through the
HarborRAG REST API's `/v1/chat` and `/v1/agent` endpoints; see
[Chat](../../chat/README.md).

### Choosing a graph tool

`graph_triplet_search`, `graph_path_search`, and `graph_subgraph_search` all need a node
selector you already hold. Only three things resolve: a `node_key`, a `logical_id`, or an
exact full `title` — titles are unset on chunk nodes and are never matched partially.
The practical selector is a `chunk_id` from `vector_search`, because chunk IDs and
`Chunk` node keys are the same value.

```python
from harborrag_mcp_server.server import McpServer, list_tools
from harborrag_runtime.sdk import HarborRAG, HarborRAGConfig

print(list_tools())
server = McpServer(runtime=HarborRAG(HarborRAGConfig()))
result = await server.call_tool(
    "vector_search",
    {"query": "publication policy", "tenant_id": "default"},
)
```

Unknown tool names raise `ValueError`.

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
Every tool call requires an explicit tenant. Retrieval propagates the caller's
principal through the runtime access context. MCP audits store argument
digests rather than raw query text.

Network transports must provide a FastMCP authentication provider.

See [Setup and Integration](setup-and-integration.md) for clients, the local
HTTP UI, bearer-token setup, tool configuration, and containers. See
[Chat](../../chat/README.md) for the chat contract and model setup.
