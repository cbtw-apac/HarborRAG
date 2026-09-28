# MCP Setup and Integration

This page covers getting the MCP server running: transports, bearer tokens, the
local status UI, tool configuration, and the container image. For what the tools
do and what arguments they take, see [MCP Tools](README.md).

## Choose a transport

| Transport | Command | Authentication | Use when |
| --- | --- | --- | --- |
| [stdio](#stdio-for-external-clients) | `harborrag-mcp` (checkout: `scripts/deployment/mcp.sh`) | None; no listener opened | An MCP client (IDE, agent) launches the server itself |
| [Local HTTP](#local-http-and-status-ui) | `harborrag-mcp --http` (checkout: `scripts/deployment/mcp.sh --http`) | Bearer token, loopback only | You want the status UI, Tool Playground, or an HTTP-capable client |
| [In-process Python](#use-from-python) | `McpServer(...)` | Caller's own runtime | An application or test needs direct control |
| [Container](#container-image) | `docker run harborrag-mcp` | None; stdio only | A client launches the server from an image |

All transports expose the same thirteen read-only tools listed in [MCP Tools](README.md)
and pass through the same policy and audit boundary. HTTP tool calls accept `reader`
or `owner` tokens with tenant grants; the local administration API requires `owner`.

Chat and agent are **not** in the MCP catalog. They are served only through the
HarborRAG REST API at `/v1/chat/completions` and `/v1/agent/completions`.

## Before you start

Bootstrap the environment files once:

```bash
scripts/deployment/dev.sh bootstrap
```

This creates the ignored checkout `env/` files at mode `0600` and generates the
local MCP bearer token. The MCP checkout wrapper reads the database, model, and
optional MCP files; it does not load the API configuration.

> **Review the placeholders before making real tool calls.**
> `HARBORRAG_SECRETS_ENCRYPTION_KEY` in `env/.env.database` ships empty. See
> [Running from a checkout, step 5](../../../developers/checkout-quick-start.md#5-create-the-env-folder).

## stdio for external clients

The installed package provides `harborrag-mcp` as the canonical command. Give
it `HARBORRAG_*` settings through the process environment or one or more
`--env-file` options. For a repository checkout, configure your MCP client to
run the convenience wrapper:

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

The wrapper runs the server in the `harborrag-mcp` container from
`deploy/compose/docker-compose.mcp.yml`, building the image the first time.
The container uses the host network, so start the data services first with
`scripts/deployment/dev.sh data`.

| Command | Behavior |
| --- | --- |
| `mcp.sh [OPTION...]` | Stdio server (`docker compose run -T`); options are forwarded to `harborrag-mcp` |
| `mcp.sh --check [OPTION...]` | One-off handshake that prints the advertised tools |
| `mcp.sh --http` | Starts the HTTP server in the background and waits for its health check |
| `mcp.sh down` / `mcp.sh logs` | Stops, or shows the logs of, the background HTTP server |
| `mcp.sh --build ...` | Rebuilds the image first; use after source or dependency changes |

Forwarded options are the `harborrag-mcp` flags (`--config`, `--transport`,
`--env-file`, ...); paths refer to the container, where the checkout's `config/`
is mounted read-only at `/app/config`. HTTP host, port, and path come from
`env/.env.mcp`.

For example, an installed deployment can run `harborrag-mcp --http --env-file
/etc/harborrag/reader.env --config /etc/harborrag/mcp.yaml` without the
repository script or checkout-specific variables.

### Environment overrides for the launcher

| Variable | Redirects |
| --- | --- |
| `DATABASE_ENV_FILE` | `env/.env.database` |
| `MODEL_ENV_FILE` | `env/.env.models` |
| `MCP_ENV_FILE` | `env/.env.mcp` |
| `HARBORRAG_MCP_IMAGE` | The image tag (default `harborrag-mcp-mcp`) |
| `HARBORRAG_MCP_STARTUP_TIMEOUT` | Seconds `--http` waits for health (default `120`) |

The database file supplies the Compose variables that build the backend
addresses and credentials; the MCP file is required and the model file is
optional. Compose reads the files as data, so no shell code in an env file runs.

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

### Tenant-bound reader keys and shared corpus access

For an internal reader deployment, set `HARBORRAG_MCP_AUTH_MODE=api_key` and
`HARBORRAG_MCP_KEYS_PATH=config/mcp_keys.yaml`. Copy
[`config/mcp_keys.example.yaml`](../../../../config/mcp_keys.example.yaml) to
that path, then create a random secret of at least 32 characters. Put its
SHA-256 hex digest in the environment variable named by `secret_hash_env`;
give the original secret to the MCP client. Keep a stable `principal_id` when
rotating the secret. The server rereads the key file on every verification, so
setting `revoked: true` takes effect without restarting. Reader keys cannot
use the owner-only configuration API.

`HARBORRAG_CORPUS_ACCESS_MODE=source_acl` is the default and requires current
source and document ACL snapshots. Set it to `tenant_shared` only for a tenant
whose published corpus is intentionally shared among its reader identities.
Set `HARBORRAG_CORPUS_SHARED_TENANT_ID=DEFAULT` to name that tenant explicitly;
other tenants continue using `source_acl` even in the same process.
The key's `tenant_id` must match each tool call's `tenant_id`; a caller cannot
select another tenant. Both HTTP modes bind to loopback; put TLS and any remote
access at a reverse proxy.

The local `HARBORRAG_MCP_BEARER_TOKEN` authenticates a connection but does not
grant corpus access by itself. To use it for an intentionally shared corpus,
also set `HARBORRAG_MCP_READER_TENANT_ID`,
`HARBORRAG_CORPUS_ACCESS_MODE=tenant_shared`, and
`HARBORRAG_CORPUS_SHARED_TENANT_ID` to the target tenant. Restart the MCP
process after changing these environment settings; an already-running server
keeps its previous access policy.

`HARBORRAG_MCP_READER_TENANT_ID` scopes what the local token reads, not what it
may administer: the owner keeps the configuration API and the browser UI, and a
request that names no tenant -- a catalog load or a tool call -- is bound to that
tenant instead of being refused. A tool call naming a different tenant is still
rejected with `403`.

Background summaries use a separate approval. For a shared `DEFAULT` corpus,
set `HARBORRAG_INGESTION_TENANT_ID=DEFAULT` and
`HARBORRAG_SUMMARY_PROCESSING_ALLOWED=true`; bump
`HARBORRAG_SUMMARY_PROCESSING_REVISION` whenever that approval changes. The
graph-build summarization switch, model, and budget must also be configured.
Reader keys do not authorize model calls. Existing source-ACL deployments keep
their snapshot-based processing rules.

Two further settings decide what a summary is and whether it can be searched:

- `HARBORRAG_SUMMARY_ENTITY_CARD_MAX_WORDS` (default `60`) is how long a
  source-entity card may be. A source entity -- a Jira issue with its comments
  and its attachments -- is the first level that spans documents, so it is the
  one worth writing as a dossier rather than a navigation hint. Raising it
  regenerates source-entity cards and leaves every other level cached, because
  the budget is part of the summary policy fingerprint.
- `HARBORRAG_SUMMARY_ENTITY_INDEX_ENABLED` (default `false`) publishes each
  accepted source-entity card as its own searchable vector point, so a question
  about a whole issue reaches the issue rather than one chunk of it. It needs a
  vector backend reachable from the summary worker and costs one embedding per
  entity per regeneration. Nothing is served from the point itself: a hit yields
  a node key, and the summary authority re-checks that binding's permissions and
  freshness before releasing any evidence.

#### Facets: the filterable part of a card

A source scope can declare **facets** -- the named facts every entity card in that
scope should carry. Each facet copies a structured field the connector already
extracted, verbatim and with no model call:

```yaml
# config/topology/graph_build.yaml
tenants:
  - tenant_id: DEFAULT
    sources:
      - source_scope_id: jira-main
        facets:
          - name: stage
            field: status               # a standard issue attribute
          - name: skill_set
            field: "Skill Set"          # display name or customfield_NNNNN
          - name: years_experience
            field: "Years of experience"
            type: integer               # stored as a number, so gte/lte filters work
```

An entity whose issue does not set a declared field simply has no value for that
facet. Facets are part of the summary policy fingerprint, so editing them
regenerates that scope's source-entity cards and nothing else.

With `HARBORRAG_SUMMARY_ENTITY_INDEX_ENABLED=true`, facets are also written to
the entity point's payload under `facet.<name>` and indexed, so they filter:

- `vector_search` / the retrieval API accept `facet.*` keys in `filters`, e.g.
  `{"facet.stage": "placed", "facet.skill_set": ["data engineering", "java"]}`. Such a
  filter selects entities on the entity lane and is taken out of the chunk
  filter, so the semantic lanes stay on instead of falling back to flat search.
- `find_entities` ranks whole entities against a question within a facet
  selection and returns each entity's summary, facets and released evidence
  ids. It is the tool for "which candidates fit this role"; `fetch_evidence` on
  the returned ids is how the answer gets cited.

#### Filtering evidence by Jira fields

Facets select whole entities and need the summary pipeline. Evidence chunks can
be filtered directly too: every chunk of a Jira issue document -- its summary,
description, and comments -- carries the issue's typed custom fields under
`fields.<key>`. An attachment (a CV) is a document of its own and does not copy
them; instead a `fields.*` filter is resolved to the matching issues first, and
the search then covers those issues' evidence *and* the evidence of every
document attached to them. Editing a field in Jira therefore takes effect as
soon as the issue is re-ingested, without reprocessing its attachments. The key
is the field's display name
normalized to snake case (`Skill Set` -> `skill_set`, `Rate Normal ($)` ->
`rate_normal`). A name that collides with another or with a reserved runtime key
falls back to the field id (`fields.customfield_10042`). Free-text custom fields
are left out; they are searchable as evidence instead.

Values keep their Jira type and are matched exactly, case included. A scalar is
an equality, a list is "any of", and a mapping of bounds is a numeric range:

```json
{
  "fields.skill_set": "Data Engineering",
  "fields.position_level": ["Medior", "Senior"],
  "fields.years_of_experience": {"gte": 3}
}
```

A filter matching more than 10,000 issues is rejected with a request to narrow
it. A `fields.*` filter cannot be combined with other `should` alternatives, and
it switches off entity-lane enrichment for that request so no chunk of a
non-matching issue is fused back in.

The map and the attachment-to-issue link are written at ingestion, so documents
ingested earlier gain them on their next reprocessing.

A card also reports its `coverage_mode`. `partial` means the connector
discovered source items -- an attachment still in OCR, or one nothing can parse
-- that had no published version when the card was written, and the card names
how many. Treat a `partial` dossier as provisional; it is superseded
automatically once the missing document lands.

### Run tools from the browser

1. Open `http://127.0.0.1:8010/`.
2. Enter the bearer token from `env/.env.mcp`.
3. Enter a tenant ID and select **Load tools**. Leave it blank to use the
   tenant bound by `HARBORRAG_MCP_READER_TENANT_ID`, if one is set.
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
> than assuming - see [MCP Tools](README.md).

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
from harborrag_runtime.composition.readers import open_reader_application
from harborrag_runtime.config.settings import RuntimeSettings

application = open_reader_application(RuntimeSettings())
server = McpServer(invoker=application.invoker, references=application.references)
await application.start()
for spec in server.list_tools():
    print(spec.name, spec.input_schema)

result = await server.call_tool(
    "vector_search",
    {"query": "publication policy", "tenant_id": "default"},
    principal_id="reader-principal",
)
await application.aclose()
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

The normal catalog contains thirteen reader tools. Chat and agent are not part
of the MCP catalog; they are served only through the HarborRAG REST API's
`/v1/chat` and `/v1/agent` endpoints.

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

The server exposes thirteen read-only evidence, document, source, and graph tools. Twelve require an
explicit tenant scope; `describe_graph` is a static schema lookup and requires none.
The traversal tools need a node
identifier the caller already holds—in practice a `chunk_id` from
`vector_search`, which is the same string as a `Chunk` node key. Advanced vector retrieval adds dense, sparse, or hybrid lanes, metadata
filters, an `observe_graph` diagnostics flag (shallow provenance only — it never loads
or ranks additional evidence; see [MCP Tools](README.md#observe_graph-is-diagnostics-not-evidence)),
and a score threshold. Calls pass
pre-execution capability, declared JSON-schema validation, and argument
budgets; post-execution result/output budgets; and an owner-only JSONL audit at
`.harborrag/mcp-audit.jsonl` (override with `HARBORRAG_MCP_AUDIT_PATH`). Audit
records contain a principal identifier, arguments digest, and outcome, never
the bearer token or raw arguments.

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

- [MCP Tools](README.md) - the thirteen tools, their arguments, and what they return
- [Extending HarborRAG](../../../developers/extending/README.md#application-and-mcp-surfaces) -
  keep service tools in `harborrag-mcp-server` and call runtime/service
  interfaces rather than provider clients
