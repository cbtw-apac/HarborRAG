# MCP Tools

`harborrag-mcp-server` currently provides an audited, policy-bounded in-process
transport. It does not yet implement stdio, SSE, or HTTP protocol transport.

## Available tools

| Tool | Arguments | Result |
| --- | --- | --- |
| `harborrag_health_check` | Empty object | In-process transport diagnostics |

```python
from harborrag_mcp_server.server import call_tool, list_tools

print(list_tools())
print(call_tool("harborrag_health_check"))
```

`call_tool` merges an optional argument dictionary with keyword arguments. Unknown tool names raise `ValueError`.

## Policy and audit status

`McpServer` records every known tool call before dispatch and enforces
`McpToolPolicy` result-count budgets. Input-schema validation, caller
authentication, and tenant permission filtering remain external integration
requirements.

A production tool/server implementation must wire those boundaries before exposing network transport.

See [Setup and Integration](setup-and-integration.md) for the current integration boundary.
