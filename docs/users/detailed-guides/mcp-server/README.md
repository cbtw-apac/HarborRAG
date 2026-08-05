# MCP Tools

`harborrag-mcp-server` provides an audited, policy-bounded FastMCP transport.

## Available tools

| Tool | Arguments | Result |
| --- | --- | --- |
| `vector_search` | Query, tenant, top-k | Default hybrid vector results |
| `vector_search_advanced` | Query, tenant, lane, filters, graph observation, threshold | Controlled vector results and diagnostics |
| `graph_neighborhood` | Tenant and a natural-language question | Merged active neighborhood plus the `chunk_id` seeds it grew from |
| `graph_triplet_search` | Tenant plus subject, predicate, or object | Active canonical triplets |
| `graph_path_search` | Tenant, start/end nodes, depth and direction | Active bounded paths |
| `graph_subgraph_search` | Tenant, start node, depth and direction | Active bounded nodes and relations |
| `chat` | Message, tenant, prompt, logical model, and bounded generation controls | Provider-neutral assistant message and usage |
| `agent` | Message, tenant, optional session, step budget, graph switch, and history | Multi-hop answer, aggregate usage, and safe tool trace |

### Choosing a graph tool

`graph_triplet_search`, `graph_path_search`, and `graph_subgraph_search` all need a node
selector you already hold. Only three things resolve: a `node_key`, a `logical_id`, or an
exact full `title` — titles are unset on chunk nodes and are never matched partially.
The practical selector is a `chunk_id` from `vector_search`, because chunk IDs and
`Chunk` node keys are the same value.

Starting from a question rather than a node, use `graph_neighborhood`: it resolves its own
seeds through the vector index and returns the merged expansion around them.


```python
from harborrag_mcp_server.server import McpServer, list_tools
from harborrag_runtime.sdk import HarborRAG, HarborRAGConfig

print(list_tools())
server = McpServer(runtime=HarborRAG(HarborRAGConfig()))
result = await server.call_tool(
    "vector_search",
    {"query": "publication policy", "tenant_id": "default"},
)

chat = await server.call_tool(
    "chat",
    {
        "message": "Explain HarborRAG in one paragraph.",
        "tenant_id": "default",
        "session_id": "general-chat",
        "prompt": "concise",
    },
)

agent = await server.call_tool(
    "agent",
    {
        "message": "Connect the release policy to its owning service.",
        "tenant_id": "default",
        "session_id": "release-review",
        "graph_search": True,
        "max_steps": 4,
    },
)
```

Unknown tool names raise `ValueError`.

## Policy and audit status

`McpServer` records every call attempt and outcome, validates each declared
JSON schema, and enforces argument, result-count, and output-size budgets.
Every tool call requires an explicit tenant. Retrieval propagates the caller's
principal through the runtime access context; chat propagates it as model
request metadata and marks the request sensitive. MCP audits store argument
digests rather than raw query, prompt, or message text.

Network transports must provide a FastMCP authentication provider.

See [Setup and Integration](setup-and-integration.md) for clients, the local
HTTP UI, bearer-token setup, tool configuration, and containers. See
[Chat](../../chat/README.md) for the chat contract and model setup.
