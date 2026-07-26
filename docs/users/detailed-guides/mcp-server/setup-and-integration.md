# MCP Setup and Integration

## Use in Python today

Instantiate the in-process server when an application or test needs direct
control:

```python
from harborrag_mcp_server.server import McpServer

server = McpServer()
for spec in server.list_tools():
    print(spec.name, spec.input_schema)

result = server.call_tool("harborrag_health_check")
```

Or use the package-level convenience functions shown in [MCP Tools](README.md).

## External clients

The package now provides a standard FastMCP stdio server. Configure a client to
run:

```bash
python -c "from harborrag_mcp_server import create_mcp_server; create_mcp_server(allow_unauthenticated_local=True).run()"
```

Only `harborrag_health_check` is currently exposed. It returns bounded service
health and no document content. Calls pass pre-execution capability and
declared JSON-schema validation and argument budgets, post-execution
result/output budgets, and an owner-only JSONL audit log at
`.harborrag/mcp-audit.jsonl` (override with `HARBORRAG_MCP_AUDIT_PATH`). Audit
records contain a principal identifier, arguments digest, and outcome, never
the bearer token or raw arguments.

The command above explicitly permits unauthenticated local stdio and opens no
listener. All other construction fails closed without a FastMCP authentication
provider. A deployment choosing HTTP or streamable HTTP must call
`create_mcp_server(auth=...)` and enforce tenant/capability authorization in
each service-backed tool. Retrieval outputs must mark source text as untrusted
data and tool descriptions must contain only developer-authored instructions.

Keep service tools in `harborrag-mcp-server` and call runtime/service interfaces
rather than provider clients. See
[Extending HarborRAG](../../../developers/extending/README.md#application-and-mcp-surfaces).
