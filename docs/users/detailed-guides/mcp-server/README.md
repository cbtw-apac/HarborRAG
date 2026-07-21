# MCP Mock Tools

`harborrag-mcp` currently provides an in-process MCP-shaped facade. It is useful for testing tool specs and dispatch, but it does not implement stdio, SSE, or HTTP protocol transport.

## Available tools

| Tool | Arguments | Result |
| --- | --- | --- |
| `harbor_health_check` | Empty object | Local engine/runtime diagnostics |
| `harbor_sample_retrieve` | Optional string `query` | A deterministic single-result retrieval response |

```python
from harborrag_mcp.server import call_tool, list_tools

print(list_tools())
print(call_tool("harbor_health_check"))
print(call_tool("harbor_sample_retrieve", {"query": "HarborRAG"}))
print(call_tool("harbor_sample_retrieve", query="HarborRAG"))
```

`call_tool` merges an optional argument dictionary with keyword arguments. Unknown tool names raise `ValueError`.

## Policy and audit status

`McpToolPolicy` defines a result-count budget and an ingestion allow/deny flag. `McpAuditLog` can record tool names. The mock server does not automatically enforce either primitive, validate the declared JSON schema, authenticate callers, or filter by tenant permissions.

A production tool/server implementation must wire those boundaries before exposing network transport.

See [Setup and Integration](setup-and-integration.md) for the current integration boundary.
