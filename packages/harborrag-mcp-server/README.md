# harborrag-mcp-server

Owns the FastMCP transport, pre-execution policy, and durable audit boundary.

## Folder ownership

```text
tools/base.py + tools/retrieval_inputs.py
tools/vector_search.py + tools/graph_search.py
chat.py + agent.py (kept for reuse, not registered as MCP tools)
server/base.py + server/server.py
```

## Team deliverables

- The shipped transport exposes six retrieval tools. Chat and agent are not
  MCP tools; they are served only through the HarborRAG REST API's
  `/v1/chat` and `/v1/agent` endpoints.
- Every attempt and outcome is durably audited with a principal identifier and
  arguments digest; raw arguments and tokens are never recorded.
- Declared input schemas plus argument, result-count, and serialized-output
  budgets are enforced.
- Ingestion-capability tools fail closed until explicitly enabled.
- Transport creation fails closed without authentication unless the caller
  explicitly selects local unauthenticated stdio.
- Service-level tools must never expose raw database/provider access or place
  retrieved document text in descriptions.

Run the standard stdio transport:

```bash
scripts/deployment/mcp.sh
```

Build the equivalent non-root container and verify its advertised registry:

```bash
docker build -f deploy/docker/Dockerfile.mcp -t harborrag-mcp .
docker run --rm harborrag-mcp --check
```

For a real stdio MCP session, run the image with an attached stdin, the
database/model environment files, and connectivity to the configured data
services. The image entrypoint is `python -m harborrag_mcp_server`, so MCP
arguments such as `--check` or `--transport http` are forwarded directly.

The launcher loads the protected database, model, API, and MCP environment files,
constructs the shared `HarborRAG` runtime, and communicates over stdin/stdout.
It is a child process launched by an MCP client, not an interactive terminal or
HTTP service. Run `scripts/deployment/mcp.sh --check` yourself to perform a real
MCP handshake and print the six advertised tool names without connecting to
providers.

Run an authenticated local Streamable HTTP endpoint and status page:

```bash
scripts/deployment/dev.sh bootstrap
scripts/deployment/mcp.sh --http
```

The browser status page is `http://127.0.0.1:8010/`, health is available at
`http://127.0.0.1:8010/healthz`, and MCP clients connect to
`http://127.0.0.1:8010/mcp` with the token in an `Authorization: Bearer`
header. Override the loopback host, port, or path with `--host`, `--port`, and
`--path`; this local static-token mode intentionally rejects non-loopback
hosts. The bootstrap command generates the token in the Git-ignored
`env/.env.mcp` file with mode `0600`; override that path with `MCP_ENV_FILE`.

## Tool Playground

Open the browser page, enter the bearer token, provide a tenant ID, and select
**Load tools**. The page builds an argument form from the effective JSON schema
and executes the selected tool with **Run tool**. Tenant-specific enablement,
defaults and limits are applied before execution, and each attempt is audited.

Chat and agent are not part of this catalog; use the HarborRAG REST API's
`/v1/chat` and `/v1/agent` endpoints instead.

The owner-only browser API is:

- `GET /api/tools?tenant_id=<tenant>` for the effective catalog;
- `POST /api/tools/call` with `{"name": "...", "arguments": {...}}` to run a tool.

The API is a local administrative convenience, not a second unprotected tool
transport. It requires the same owner bearer token as configuration editing.

## Tool configuration

The versioned configuration lives at `config/mcp.yaml` by default. Set
`HARBORRAG_MCP_CONFIG_PATH` or pass `--config` after `--http` to select another
file. The configuration controls:

- global policy budgets;
- globally enabled tools;
- optional argument defaults and numeric upper limits;
- per-tenant enabled state, defaults, and limits.

Required fields and `tenant_id` can never receive defaults, unknown tools and
fields are rejected, and configured limits cannot exceed each tool's compiled
safety ceiling. Tenant IDs are canonicalized before policy lookup and execution,
and configuration tenant keys may not contain surrounding whitespace. These environment variables override the file without being
persisted back into it:

- `HARBORRAG_MCP_MAX_RESULTS`
- `HARBORRAG_MCP_MAX_ARGUMENT_BYTES`
- `HARBORRAG_MCP_MAX_OUTPUT_BYTES`
- `HARBORRAG_MCP_DISABLED_TOOLS` (comma-separated tool names)

In HTTP mode, enter the bearer token in the status page and use **Load**,
**Save**, or **Reload YAML**. The owner-only API is `GET/PUT /api/config` and
`POST /api/config/reload`. Saves use revision checks and atomic replacement;
audit records contain only revision hashes, never configuration values.

Defaults, limits, budgets, and tenant overrides are enforced immediately.
Changes to global advertised tool schemas or enabled tools set
`restart_required=true`; restart the MCP process so connected clients receive
the new advertised catalog.

Network transports must be constructed with a FastMCP authentication provider.
MCP tool calls require an authenticated token carrying `role=owner` and an
explicit `tenants` claim containing the requested tenant. Global owners must
carry the deliberate wildcard claim `tenants=["*"]`.
The explicit local override is for stdio only and opens no listener.


## Package tests

Tests for this package live in:

```text
packages/harborrag-mcp-server/tests/
```

Run from the repository root:

```bash
pytest packages/harborrag-mcp-server/tests
```

Keep new tests in this folder when adding or changing behavior owned by this package.
