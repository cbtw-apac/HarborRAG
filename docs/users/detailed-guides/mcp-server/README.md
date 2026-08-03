# MCP Tools

`harborrag-mcp-server` provides an audited, policy-bounded FastMCP transport.

## Available tools

| Tool | Arguments | Result |
| --- | --- | --- |
| `vector_search` | Query, tenant, top-k | Default hybrid vector results |
| `vector_search_advanced` | Query, tenant, lane, filters, graph observation, threshold | Controlled vector results and diagnostics |
| `graph_triplet_search` | Tenant plus subject, predicate, or object | Active canonical triplets |
| `graph_path_search` | Tenant, start/end nodes, depth and direction | Active bounded paths |
| `graph_subgraph_search` | Tenant, start node, depth and direction | Active bounded nodes and relations |
| `chat` | Message, tenant, prompt, logical model, and bounded generation controls | Provider-neutral assistant message and usage |

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
        "prompt": "concise",
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
