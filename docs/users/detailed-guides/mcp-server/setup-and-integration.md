# MCP Setup and Integration

## Use in Python today

Instantiate the in-process server when an application or test needs direct
control:

```python
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

Or use the package-level convenience functions shown in [MCP Tools](README.md).

## External clients

The package now provides a standard FastMCP stdio server. Configure a client to
run:

```bash
scripts/deployment/dev.sh bootstrap
scripts/deployment/mcp.sh
```

Bootstrap creates the ignored database, model, API, and MCP environment files.
Review their placeholders before real tool calls. It also generates the local
MCP bearer token and protects `env/.env.mcp` with mode `0600`.

Use `scripts/deployment/mcp.sh --check` to validate configuration and list
the registered tools without opening provider connections. The check opens an
in-memory client session, performs the MCP initialization handshake, and asks
the server for its tools.

The normal catalog contains five retrieval tools and one `chat` tool. Chat is
available when the launcher can compose the shared HarborRAG runtime and load
the `chat` family from `config/models.yaml`.

Do not run the stdio command as an interactive service. An MCP client must
launch it with stdin and stdout connected to pipes. A direct terminal launch
now exits with that guidance instead of appearing to hang. The command does not
open a port; use `--check` for a manual readiness check.

## Local Streamable HTTP and status UI

Start the loopback-only HTTP transport after bootstrap:

```bash
scripts/deployment/mcp.sh --http
```

This exposes:

- status UI: `http://127.0.0.1:8010/`
- health: `http://127.0.0.1:8010/healthz`
- authenticated MCP: `http://127.0.0.1:8010/mcp`

Configure an HTTP-capable MCP client with that MCP URL and the value of
`HARBORRAG_MCP_BEARER_TOKEN` as its bearer token. For example:

```python
from fastmcp import Client

client = Client("http://127.0.0.1:8010/mcp", auth="<token>")
```

The built-in page never renders the bearer token. Its Tool Playground displays
retrieved content only after an authenticated owner explicitly invokes a tool.
Static tokens are for local development only. The launcher rejects non-loopback
binding; remote or production exposure requires TLS and a production JWT/JWKS
token verifier.

### Run tools from the browser

Open `http://127.0.0.1:8010/`, enter the bearer token from `env/.env.mcp`, enter
a tenant ID, and select **Load tools**. The page loads that tenant's effective
catalog and generates argument controls from each tool's JSON schema. Select
**Run tool** to execute through the same configuration, policy, runtime access,
and audit boundaries as the MCP transport. Results are rendered as formatted
text, never injected as HTML.

Select `chat`, provide `message` and `tenant_id`, and optionally select a stored
prompt or logical model to use chat from the browser. The playground sends the
call to the local MCP server; it does not call a model provider directly or
expose provider credentials to the page.

### Configure tools from the browser

Open `http://127.0.0.1:8010/`, enter the same value used for
`HARBORRAG_MCP_BEARER_TOKEN`, and select **Load**. The editor exposes the
validated JSON representation of `config/mcp.yaml`; **Save** atomically writes
it back as YAML, while **Reload YAML** discards in-memory changes and reloads
the file.

Each tool supports three controls:

```yaml
tools:
  vector_search:
    enabled: true
    defaults:
      top_k: 5
    limits:
      top_k: 10
```

Chat uses the same controls:

```yaml
tools:
  chat:
    enabled: true
    defaults:
      prompt: default
      temperature: 0.2
      max_tokens: 1024
    limits:
      temperature: 2.0
      max_tokens: 32768
```

These values control the public tool contract. Model provider, endpoint, and
credential settings remain in `config/models.yaml` and the process
environment; they cannot be configured through the MCP UI.

Tenant overrides merge over global values:

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

The API requires the owner bearer token:

- `GET /api/config` returns source and effective settings, revision, active
  environment overrides, and restart state.
- `PUT /api/config` accepts `configuration` and `expected_revision`.
- `POST /api/config/reload` reloads and validates the YAML file.
- `GET /api/tools?tenant_id=<tenant>` returns the tenant-effective tool catalog.
- `POST /api/tools/call` executes a named tool with an `arguments` object.

Defaults, numeric limits, policy budgets, and tenant controls are enforced on
new calls immediately. FastMCP snapshots globally advertised tools and schemas
at process start, so global tool/schema changes report `restart_required=true`.
Restart the process after saving to refresh what clients see in `tools/list`.

The configuration fails closed: it rejects unknown tools or fields, invalid
types, defaults for required or tenant identity fields, stale revisions, and
limits above compiled safety ceilings. Change audits store only old/new
revision hashes and the authenticated principal.

Environment overrides are applied after the file and are not written back:

```text
HARBORRAG_MCP_MAX_RESULTS
HARBORRAG_MCP_MAX_ARGUMENT_BYTES
HARBORRAG_MCP_MAX_OUTPUT_BYTES
HARBORRAG_MCP_DISABLED_TOOLS
HARBORRAG_MCP_CONFIG_PATH
```

The server exposes five retrieval tools—`vector_search`,
`vector_search_advanced`, `graph_triplet_search`, `graph_path_search`, and
`graph_subgraph_search`—plus `chat`. Every tool accepts an explicit tenant
scope. Advanced vector retrieval adds dense, sparse, or hybrid lanes, metadata
filters, graph observation control, and a score threshold. Calls pass
pre-execution capability, declared JSON-schema validation, and argument
budgets; post-execution result/output budgets; and an owner-only JSONL audit at
`.harborrag/mcp-audit.jsonl` (override with `HARBORRAG_MCP_AUDIT_PATH`). Audit
records contain a principal identifier, arguments digest, and outcome, never
the bearer token or raw arguments.

Chat accepts a message, the `default` or `concise` stored prompt, a configured
logical model, and bounded generation controls. It never accepts provider API
keys, base URLs, custom headers, tools, or arbitrary provider parameters. Chat
requests are marked sensitive and raw messages or responses are not written to
the MCP audit log.

## Container image

Build and validate the dedicated MCP image from the repository root:

```bash
docker build -f deploy/docker/Dockerfile.mcp -t harborrag-mcp .
docker run --rm harborrag-mcp --check
```

The image contains the runtime, model/retrieval adapter extras, packaged prompt
templates, `config/models.yaml`, and `config/mcp.yaml`. It runs as the non-root
`harborrag` user, stores the audit log under its writable home directory, and
uses stdio by default. An MCP client launching the container must keep stdin
open with `-i` and provide the protected model/database environment plus
reachable data-service endpoints.

The checked-in authenticated HTTP launcher is intentionally loopback-only. Run
`scripts/deployment/mcp.sh --http` on the host for the status/configuration UI;
do not publish an unauthenticated or remotely bound development MCP endpoint
from a container.

The command above explicitly permits unauthenticated local stdio and opens no
listener. All other construction fails closed without a FastMCP authentication
provider. A production deployment choosing remote HTTP must provide TLS, a
production JWT/JWKS verifier, and tenant/capability authorization for every
service-backed tool. Retrieved source text and chat input are untrusted data;
tool descriptions and stored prompt templates contain only developer-authored
instructions.

Keep service tools in `harborrag-mcp-server` and call runtime/service interfaces
rather than provider clients. See
[Extending HarborRAG](../../../developers/extending/README.md#application-and-mcp-surfaces).
