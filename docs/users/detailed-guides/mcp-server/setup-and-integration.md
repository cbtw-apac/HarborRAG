# MCP Setup and Integration

## Use in Python today

Instantiate the mock server when tests need direct control:

```python
from harborrag_mcp.server import MockMcpServer

server = MockMcpServer()
for spec in server.list_tools():
    print(spec.name, spec.input_schema)

result = server.call_tool("harbor_sample_retrieve", {"query": "HarborRAG"})
```

Or use the package-level convenience functions shown in [MCP Mock Tools](README.md).

## External clients

There is no command to add to an IDE or desktop MCP configuration yet. `BaseMcpServer` defines only Python `list_tools()` and `call_tool()` methods, and `MockMcpServer` dispatches them in process.

An external integration still needs:

1. a protocol transport translating MCP `tools/list` and `tools/call` messages;
2. real service-backed tools instead of the fixed mock retrieval result;
3. JSON-schema input validation and normalized protocol errors;
4. identity, tenant, permission, and budget enforcement;
5. automatic audit recording and safe observability;
6. lifecycle and shutdown handling.

Keep the transport in `harborrag-mcp` and call runtime/service interfaces rather than provider clients. See [Extending HarborRAG](../../../developers/extending/README.md#application-and-mcp-surfaces).
