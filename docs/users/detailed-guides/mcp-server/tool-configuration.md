# Configure MCP tools

Set tool defaults and limits, inspect the management API, or call the same
tools from a Python application.

- [Set defaults and limits by tool or tenant](#tool-configuration)
- [Read the management API](#management-api)
- [Call tools from Python](#use-from-python)

## Tool configuration

`config/mcp/server.yaml` decides which tools are enabled and their defaults and
limits, globally and per tenant. Model provider, endpoint and credential
settings stay in `config/models/models.yaml` and the process environment; the
MCP UI cannot change them.

### Per-tool controls

Each tool takes three controls: `enabled`, `defaults` and `limits`.

```yaml
tools:
  vector_search:
    enabled: true
    defaults:
      top_k: 5
    limits:
      top_k: 10
```

### Tenant overrides

Settings under a tenant ID in the `tenants:` map merge over the global values.
The shipped file has no tenant overrides (`tenants: {}`):

```yaml
tenants:
  engineering:
    tools:
      vector_search:
        defaults:
          top_k: 8
  restricted:
    tools:
      graph_subgraph_search:
        enabled: false
```

This map is the only place for per-tenant tool controls. One runtime setting
also reaches the tools: a tenant's `retrieval.top_k` replaces the
`vector_search` default `top_k`, still capped by the tool's limits; see
[Runtime settings](../../configuration/runtime-settings.md#settings-and-their-levels).

### Environment variable overrides

These four variables override values from the file. They are applied after the
file is loaded and are never written back:

```text
HARBORRAG_MCP_MAX_RESULTS
HARBORRAG_MCP_MAX_ARGUMENT_BYTES
HARBORRAG_MCP_MAX_OUTPUT_BYTES
HARBORRAG_MCP_DISABLED_TOOLS
```

`HARBORRAG_MCP_CONFIG_PATH` is different: it selects *which file* is loaded
rather than overriding a value inside one. The server takes the first that
exists:

```text
HARBORRAG_MCP_CONFIG_PATH  →  config/mcp/server.yaml  →  packaged defaults/mcp.yaml
```

### When changes take effect

| Change | Effect |
| --- | --- |
| Defaults, numeric limits, policy budgets, tenant controls | Enforced on new calls immediately |
| Global tool or schema changes | Reported as `restart_required=true` |

FastMCP fixes the advertised tools and schemas when the process starts, so
restart the server after saving a global change to refresh what clients see in
`tools/list`.

### Validation

The configuration fails closed. It rejects:

- unknown tools or fields
- invalid types
- defaults for required or tenant identity fields
- stale revisions
- limits above compiled safety ceilings

One exception keeps an upgrade from stopping the server. When
`config/mcp/server.yaml` is read at startup or on reload, a numeric default or
limit above a tool's compiled safety ceiling is lowered to that ceiling and
logged as a `WARNING` that names the tool and field. For example, the graph
tools cap `max_depth` at 4 and `max_paths` at 5, so an alpha1 file's
`max_depth: 8` and `max_paths: 20` load as 4 and 5. Lower the values in the
file to silence the warning; saving through the editor still rejects them.

Configuration change audits store only the old and new revision hashes and the
authenticated principal.

Effective defaults come from `config/mcp/server.yaml` and can differ from the
advertised schema defaults. Read `GET /api/tools?tenant_id=<tenant>` rather
than assuming.

## Management API

The local HTTP server also answers a small JSON API, used by the status page.

| Endpoint | Token | Purpose |
| --- | --- | --- |
| `GET /api/config` | Owner | Source and effective settings, revision, active environment overrides, restart state, and whether the file can be saved (`writable`) |
| `PUT /api/config` | Owner | Saves a configuration; takes `configuration` and `expected_revision`. Answers `409` when the revision is stale or the file is read-only |
| `POST /api/config/reload` | Owner | Reloads and validates the YAML file |
| `GET /api/tools?tenant_id=<tenant>` | Reader or owner | The tenant's effective tool catalog. Without `tenant_id`, also the tenants the caller may choose |
| `POST /api/tools/call` | Reader or owner | Runs a named tool with an `arguments` object |

A reader key reaches only its own tenant through the two tool endpoints.

## Use from Python

To connect to a running HTTP server from Python, use FastMCP's `Client`.
Provide the token via the environment and use the MCP endpoint described in
[HTTP client setup](clients.md#connect-an-http-client).

```python
import asyncio
import os

from fastmcp import Client


async def main() -> None:
    async with Client(
        "http://127.0.0.1:8010/mcp", auth=os.environ["HARBORRAG_MCP_TOKEN"]
    ) as client:
        print([tool.name for tool in await client.list_tools()])


asyncio.run(main())
```

### Run in process

An application or test can run the server in process. The module-level
`list_tools()` returns the tool catalog as plain dictionaries, with no backend
connection:

```python
from harborrag_mcp_server.server import list_tools

for tool in list_tools():
    print(tool["name"])
```

To call tools, build the server on a reader application and give it an audit
log. It reads the same
`HARBORRAG_*` settings as the server, so the database, object store, vector
store and model variables must be set:

```python
import asyncio
from pathlib import Path

from harborrag_mcp_server.audit import McpAuditLog
from harborrag_mcp_server.server import McpServer
from harborrag_runtime.composition.readers import open_reader_application
from harborrag_runtime.config.settings import RuntimeSettings


async def main() -> None:
    application = open_reader_application(RuntimeSettings())
    server = McpServer(
        invoker=application.invoker,
        references=application.references,
        audit=McpAuditLog(path=Path(".harborrag/mcp-audit.jsonl")),
    )
    await application.start()
    try:
        result = await server.call_tool(
            "vector_search",
            {"query": "publication policy", "tenant_id": "DEFAULT"},
            principal_id="reader-principal",
        )
        print(result)
    finally:
        await application.aclose()


asyncio.run(main())
```

`server.list_tools()` returns `ToolSpec` objects with `.name` and
`.input_schema`. An unknown tool name raises `ValueError`.
