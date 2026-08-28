# MCP Setup and Integration

This page covers getting the MCP server running: transports, bearer tokens, the
local status UI, tool configuration, and the container image. For what the tools
do and what arguments they take, see [MCP Tools](README.md).

## Choose a transport

| Transport | Command | Authentication | Use when |
| --- | --- | --- | --- |
| [stdio](#stdio-for-external-clients) | `scripts/deployment/mcp.sh` | None; no listener opened | An MCP client (IDE, agent) launches the server itself |
| [Local HTTP](#local-http-and-status-ui) | `scripts/deployment/mcp.sh --http` | Bearer token, loopback only | You want the status UI, Tool Playground, or an HTTP-capable client |
| [In-process Python](#use-from-python) | `McpServer(...)` | Caller's own runtime | An application or test needs direct control |
| [Container](#container-image) | `docker run harborrag-mcp` | None; stdio only | A client launches the server from an image |

All transports expose the same four read-only retrieval tools -
`vector_search`, `graph_triplet_search`, `graph_path_search`, and
`graph_subgraph_search` - and pass through the same policy and audit boundary.

Chat and agent are **not** in the MCP catalog. They are served only through the
HarborRAG REST API at `/v1/chat/completions` and `/v1/agent/completions`.

## Before you start

Bootstrap the environment files once:

```bash
scripts/deployment/dev.sh bootstrap
```

This creates all seven ignored `env/` files at mode `0600` - including the
database, model, API, and MCP files that `mcp.sh` needs - and generates the
local MCP bearer token.

> **Review the placeholders before making real tool calls.**
> `HARBORRAG_SECRETS_ENCRYPTION_KEY` in `env/.env.database` ships empty. See
> [Quick Start step 5](../../../getting-started/quick-start.md#5-create-the-env-folder).

## stdio for external clients

The package provides a standard FastMCP stdio server. Configure your MCP client
to run:

```bash
scripts/deployment/mcp.sh
```

> **Do not run this as an interactive service.** An MCP client must launch it
> with stdin and stdout connected to pipes. A direct terminal launch exits with
> that guidance rather than appearing to hang. The command opens no port - use
> `--check` for a manual readiness check.

### Validate without connecting

```bash
scripts/deployment/mcp.sh --check
```

This validates configuration and lists the registered tools without opening
provider connections. It opens an in-memory client session, performs the MCP
initialization handshake, and asks the server for its tools.

### Flags

`mcp.sh` itself accepts only `--check`, `--http`, and `-h`. Server flags are
pass-through and work **only after** `--http`:

| Flag | Accepted by | Notes |
| --- | --- | --- |
| `--check`, `--http`, `-h` | `mcp.sh` | |
| `--host`, `--port`, `--path`, `--config`, `--transport` | the server, after `--http` | `mcp.sh --check --config X` is rejected as an unknown option |

To select a configuration file in `--check` or stdio mode, set
`HARBORRAG_MCP_CONFIG_PATH` instead of passing `--config`.

### Environment overrides for the launcher

| Variable | Redirects |
| --- | --- |
| `DATABASE_ENV_FILE` | `env/.env.database` |
| `MODEL_ENV_FILE` | `env/.env.models` |
| `API_ENV_FILE` | `env/.env.api` |
| `MCP_ENV_FILE` | `env/.env.mcp` |
| `HARBORRAG_MCP_PYTHON_BIN` | The interpreter used to start the server |

`mcp.sh` hard-requires `env/.env.api` even though it starts no API.

## Local HTTP and status UI

Start the loopback-only HTTP transport after bootstrap:

```bash
scripts/deployment/mcp.sh --http
```

### Endpoints

| Endpoint | Authentication | Purpose |
| --- | --- | --- |
| `http://127.0.0.1:8010/` | **None** | Status UI, Tool Playground, configuration editor |
| `http://127.0.0.1:8010/healthz` | **None** | Returns transport, MCP path, authentication mode, and tool count |
| `http://127.0.0.1:8010/mcp` | Bearer token, scope `mcp:read` | The MCP transport itself |

The first two are deliberately open so a loopback health check needs no
credential. The page never renders the token, and every tool call and
configuration change behind it does require one.

### Connect an HTTP client

Use the MCP URL with the value of `HARBORRAG_MCP_BEARER_TOKEN` as the bearer
token:

```python
from fastmcp import Client

client = Client("http://127.0.0.1:8010/mcp", auth="<token>")
```

### Run tools from the browser

1. Open `http://127.0.0.1:8010/`.
2. Enter the bearer token from `env/.env.mcp`.
3. Enter a tenant ID and select **Load tools**.
4. Select **Run tool**.

The page loads that tenant's effective catalog and generates argument controls
from each tool's JSON schema. Running a tool executes through the same
configuration, policy, runtime access, and audit boundaries as the MCP
transport. Results are rendered as formatted text, never injected as HTML, and
retrieved content appears only after an authenticated owner explicitly invokes a
tool.

### Configure tools from the browser

1. Open `http://127.0.0.1:8010/`.
2. Enter the same value used for `HARBORRAG_MCP_BEARER_TOKEN`.
3. Select **Load**.

The editor exposes the validated JSON representation of `config/mcp.yaml`.
**Save** atomically writes it back as YAML; **Reload YAML** discards in-memory
changes and reloads the file.

## Tool configuration

### Per-tool controls

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

These values control the public tool contract. Model provider, endpoint, and
credential settings remain in `config/models.yaml` and the process environment;
they cannot be configured through the MCP UI.

### Tenant overrides

Tenant settings merge over global values:

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

### Environment variable overrides

These four override values from the file. They are applied after the file is
loaded and are never written back:

```text
HARBORRAG_MCP_MAX_RESULTS
HARBORRAG_MCP_MAX_ARGUMENT_BYTES
HARBORRAG_MCP_MAX_OUTPUT_BYTES
HARBORRAG_MCP_DISABLED_TOOLS
```

`HARBORRAG_MCP_CONFIG_PATH` is different: it selects *which file* to load rather
than overriding a value inside one. Resolution order:

```text
HARBORRAG_MCP_CONFIG_PATH  →  config/mcp.yaml  →  packaged defaults/mcp.yaml
```

### When changes take effect

| Change | Effect |
| --- | --- |
| Defaults, numeric limits, policy budgets, tenant controls | Enforced on new calls immediately |
| Global tool or schema changes | Reported as `restart_required=true` |

FastMCP snapshots globally advertised tools and schemas at process start, so
restart the process after saving to refresh what clients see in `tools/list`.

### Validation

The configuration fails closed. It rejects:

- unknown tools or fields
- invalid types
- defaults for required or tenant identity fields
- stale revisions
- limits above compiled safety ceilings

Change audits store only old/new revision hashes and the authenticated
principal.

> Effective defaults come from `config/mcp.yaml` and can differ from the
> advertised schema defaults. Read `GET /api/tools?tenant_id=<tenant>` rather
> than assuming - see [MCP Tools](README.md#the-four-tools).

## Management API

All endpoints require the owner bearer token.

| Endpoint | Purpose |
| --- | --- |
| `GET /api/config` | Source and effective settings, revision, active environment overrides, restart state |
| `PUT /api/config` | Accepts `configuration` and `expected_revision` |
| `POST /api/config/reload` | Reloads and validates the YAML file |
| `GET /api/tools?tenant_id=<tenant>` | The tenant-effective tool catalog |
| `POST /api/tools/call` | Executes a named tool with an `arguments` object |

## Use from Python

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

Or use the package-level convenience functions shown in
[MCP Tools](README.md#calling-the-tools-in-python).

## Container image

Build and validate the dedicated MCP image from the repository root:

```bash
docker build -f deploy/docker/Dockerfile.mcp -t harborrag-mcp .
docker run --rm harborrag-mcp --check
```

The image contains the runtime, model/retrieval adapter extras, packaged prompt
templates, `config/models.yaml`, and `config/mcp.yaml`. It runs as the non-root
`harborrag` user, stores the audit log under its writable home directory, and
uses stdio by default.

An MCP client launching the container must keep stdin open with `-i` and provide
the protected model/database environment plus reachable data-service endpoints.

> The checked-in authenticated HTTP launcher is intentionally loopback-only. Run
> `scripts/deployment/mcp.sh --http` on the host for the status and
> configuration UI; do not publish an unauthenticated or remotely bound
> development MCP endpoint from a container.

## Security boundaries

- **Loopback only.** The launcher rejects non-loopback binding. Static tokens
  are for local development only.
- **Local stdio is the one unauthenticated path.** `docker run --rm
  harborrag-mcp --check` explicitly permits unauthenticated local stdio and
  opens no listener. All other construction fails closed without a FastMCP
  authentication provider.
- **Remote or production HTTP requires** TLS, a production JWT/JWKS token
  verifier, and tenant/capability authorization for every service-backed tool.
- **Retrieved source text and chat input are untrusted data.** Tool descriptions
  and stored prompt templates contain only developer-authored instructions.

Every call passes a capability check, JSON-schema validation, argument budgets,
result and output budgets, and an owner-only JSONL audit at
`.harborrag/mcp-audit.jsonl` (override with `HARBORRAG_MCP_AUDIT_PATH`). Audit
records contain a principal identifier, an arguments digest, and the outcome -
never the bearer token or raw arguments. See
[Policy and audit](README.md#policy-and-audit) for the full sequence.

## Next

- [MCP Tools](README.md) - the four tools, their arguments, and what they return
- [Extending HarborRAG](../../../developers/extending/README.md#application-and-mcp-surfaces) -
  keep service tools in `harborrag-mcp-server` and call runtime/service
  interfaces rather than provider clients
