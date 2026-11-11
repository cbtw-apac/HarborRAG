# MCP Setup and Integration

Start the HarborRAG MCP server and connect an AI assistant or IDE to it, such
as Claude Desktop, Cursor or VS Code. The assistant can then search and cite
your indexed content through HarborRAG's tools. This page also covers reader
keys for other people, the local status page, tool configuration and the
container image. What each tool does is in [MCP Tools](README.md).

An **MCP client** is the application that calls the tools; a **transport** is
how it reaches the server: as a child process over standard input and output
(stdio), or over HTTP.

## Choose a transport

| Transport | Command | Authentication | Use when |
| --- | --- | --- | --- |
| [stdio](#stdio-for-external-clients) | `harborrag-mcp` (checkout: `scripts/deployment/mcp.sh`) | None; no network listener | An MCP client (IDE, assistant, agent) starts the server itself |
| [Local HTTP](#local-http-and-status-ui) | `harborrag-mcp --http` (checkout: `scripts/deployment/mcp.sh --http`) | Bearer token, loopback only | You want the status page, the Tool Playground, or an HTTP-capable client |
| [In-process Python](#use-from-python) | `McpServer(...)` | The caller's own runtime | An application or test needs direct control |
| [Container](#container-image) | `docker run harborrag-mcp` | None; stdio only | A client starts the server from an image |

If this is your first time, use stdio from a repository checkout and follow
[Connect an MCP client](#connect-an-mcp-client).

All transports expose the same eleven read-only tools and pass through the same
policy and audit checks. Over HTTP, tool calls accept `reader` and `owner`
tokens that hold the tenant; changing the server's configuration needs the
`owner` token. Chat is **not** in the MCP catalog; it is the HarborRAG HTTP API
at `/v1/chat/completions`.

## Before you start

1. Set up a checkout with some ingested content by following the
   [Quick Start](../../../getting-started/quick-start.md). The MCP server reads
   the databases and the object store, so those data services must be running;
   `scripts/deployment/dev.sh data` starts only them.
2. Create the environment files once, if the Quick Start has not already:

   ```bash
   scripts/deployment/dev.sh bootstrap
   ```

   This creates the git-ignored `env/` files with mode `0600` and writes a
   random local bearer token to `HARBORRAG_MCP_BEARER_TOKEN` in
   `env/.env.mcp`. The MCP wrapper reads `env/.env.database` and
   `env/.env.mcp`, `env/.env.models` when it exists, and only `HARBORRAG_ENV`
   from `env/.env.api`. The server connects with the `POSTGRES_*` and
   `MINIO_ROOT_*` accounts of `env/.env.database`.
3. Decide what the server may read. By default
   (`HARBORRAG_CORPUS_ACCESS_MODE=source_acl`) a caller sees only the documents
   whose source permissions it holds. For a development stack whose whole
   corpus everyone may read, uncomment these three lines in `env/.env.mcp`:

   ```bash
   HARBORRAG_MCP_READER_TENANT_ID=DEFAULT
   HARBORRAG_CORPUS_ACCESS_MODE=tenant_shared
   HARBORRAG_CORPUS_SHARED_TENANT_ID=DEFAULT
   ```

   `DEFAULT` is the tenant your checkout's connections are imported into. A
   running server keeps its old policy until you restart it. The details are
   in [Shared corpus access](#shared-corpus-access).

The MCP server never needs `HARBORRAG_SECRETS_ENCRYPTION_KEY`: it never
decrypts stored connector secrets and is not given the key.

## stdio for external clients

With stdio, the MCP client starts the server as a child process and talks to it
over standard input and output; no port is opened. An installed package
provides the `harborrag-mcp` command, which reads `HARBORRAG_*` settings from
the process environment or from one or more `--env-file` options. In a
checkout, use the wrapper `scripts/deployment/mcp.sh`, which runs the server in
Docker with the checkout's `env/` files.

Check that the server starts and lists its tools, without opening any provider
connection:

```bash
scripts/deployment/mcp.sh --check
```

The first run builds the image, which takes a few minutes. The check performs
the MCP initialization handshake in memory and prints the tool names:

```text
["vector_search", "fetch_evidence", "get_document_context", "list_sources", "describe_graph", "graph_triplet_search", "graph_subgraph_search", "graph_path_search", "resolve_graph_nodes", "list_documents", "find_entities"]
```

Do not run `scripts/deployment/mcp.sh` without `--check` in a terminal. An MCP
client must start it with stdin and stdout connected to pipes; started from a
terminal, it exits with that guidance instead of waiting for a client that
never comes.

### Run the wrapper and forward server options

The wrapper runs the server in the `harborrag-mcp` container from
`deploy/compose/docker-compose.mcp.yml` and builds the image the first time.
The container uses the host network, so start the data services first.

| Command | Behavior |
| --- | --- |
| `mcp.sh [OPTION...]` | Stdio server (`docker compose run -T`); options are forwarded to `harborrag-mcp` |
| `mcp.sh --check [OPTION...]` | One-off handshake that prints the advertised tools |
| `mcp.sh --http` | Starts the HTTP server in the background and waits for its health check |
| `mcp.sh down` / `mcp.sh logs` | Stops, or shows the logs of, the background HTTP server |
| `mcp.sh --build ...` | Rebuilds the image first; use it after source or dependency changes |

Forwarded options are the `harborrag-mcp` flags (`--config`, `--transport`,
`--env-file`, ...). Paths refer to the container, where the checkout's
`config/` is mounted read-only at `/app/config`. The HTTP host, port and path
come from `env/.env.mcp`.

On a server where the package is installed instead of a checkout, run the
command directly, without the wrapper or checkout-specific variables:

```bash
harborrag-mcp --http --env-file /etc/harborrag/reader.env --config /etc/harborrag/mcp.yaml
```

### Override launcher settings with environment variables

| Variable | Redirects |
| --- | --- |
| `DATABASE_ENV_FILE` | `env/.env.database` |
| `MODEL_ENV_FILE` | `env/.env.models` |
| `MCP_ENV_FILE` | `env/.env.mcp` |
| `API_ENV_FILE` | `env/.env.api`, read only for `HARBORRAG_ENV` |
| `HARBORRAG_MCP_IMAGE` | The image tag (default `harborrag-mcp-mcp`) |
| `HARBORRAG_MCP_STARTUP_TIMEOUT` | Seconds `--http` waits for the health check (default `120`) |

The database file supplies the Compose variables that build the backend
addresses and credentials; the MCP file is required and the model file is
optional. Compose reads the files as data, so no shell code in an env file
runs.

## Connect an MCP client

Register the checkout's wrapper as a stdio server in your client. Replace
`/path/to/HarborRAG` with the absolute path of your checkout. The client then
starts the server itself whenever it needs the tools; the data services must
already be running. The wrapper is a Bash script that calls Docker, so the
client must be able to run both.

Each block below holds only the HarborRAG values; where the file lives and how
to reload it are in the client's own MCP documentation, linked in each block.

<details markdown="1">
<summary>Claude Desktop</summary>

Add the server to `claude_desktop_config.json`, then restart Claude Desktop.
See [Connect to local MCP servers](https://modelcontextprotocol.io/docs/develop/connect-local-servers)
for where the file is and how to read the logs.

```json
{
  "mcpServers": {
    "harborrag": {
      "command": "/path/to/HarborRAG/scripts/deployment/mcp.sh"
    }
  }
}
```

</details>

<details markdown="1">
<summary>Cursor</summary>

Add the server to `.cursor/mcp.json` in a project, or to `~/.cursor/mcp.json`
for every project. See [Cursor MCP documentation](https://cursor.com/docs/mcp).

```json
{
  "mcpServers": {
    "harborrag": {
      "type": "stdio",
      "command": "/path/to/HarborRAG/scripts/deployment/mcp.sh"
    }
  }
}
```

</details>

<details markdown="1">
<summary>VS Code</summary>

Add the server to `.vscode/mcp.json` in your workspace. VS Code's format uses
`servers` instead of `mcpServers`. See the
[VS Code MCP configuration reference](https://code.visualstudio.com/docs/agents/reference/mcp-configuration).

```json
{
  "servers": {
    "harborrag": {
      "type": "stdio",
      "command": "/path/to/HarborRAG/scripts/deployment/mcp.sh"
    }
  }
}
```

</details>

<details markdown="1">
<summary>Other clients</summary>

Any client that starts stdio servers takes the same command,
`/path/to/HarborRAG/scripts/deployment/mcp.sh`, with no arguments. A client
that connects over HTTP instead uses `http://127.0.0.1:8010/mcp` with an
`Authorization: Bearer <token>` header; start that server and find the token in
[Local HTTP and status UI](#local-http-and-status-ui). Where to enter either
value is in your client's MCP documentation.

</details>

When it worked, the client lists HarborRAG's eleven tools, the same names that
`mcp.sh --check` printed. Every tool except `describe_graph` needs a tenant, so
tell the assistant to use tenant `DEFAULT`, or the tenant your content is in.
If the client shows no tools, run `scripts/deployment/mcp.sh --check` in a
terminal to see whether the server itself starts.

## Local HTTP and status UI

Start the HTTP transport, bound to loopback only, in the background:

```bash
scripts/deployment/mcp.sh --http
```

When the server is healthy, the wrapper prints
`HarborRAG MCP status UI: http://127.0.0.1:8010/` and how to stop it
(`scripts/deployment/mcp.sh down`).

### Local HTTP endpoints

| Endpoint | Authentication | Purpose |
| --- | --- | --- |
| `http://127.0.0.1:8010/` | **None** | Status page, Tool Playground, configuration editor |
| `http://127.0.0.1:8010/healthz` | **None** | Health check: `status`, `service`, `transport`, `mcp_path`, `authentication` and `tool_count` |
| `http://127.0.0.1:8010/mcp` | Bearer token, scope `mcp:read` | The MCP transport itself |

The first two are deliberately open so a loopback health check needs no
credential. The page never shows the token, and every tool call and
configuration change behind it needs one.

### Connect an HTTP client

Give the client the MCP URL and, as its bearer token, the value of
`HARBORRAG_MCP_BEARER_TOKEN` from `env/.env.mcp`. For example, with the
FastMCP Python client:

```python
from fastmcp import Client

client = Client("http://127.0.0.1:8010/mcp", auth="<token>")
```

### Run tools from the browser

The Tool Playground on the status page runs any tool by hand:

1. Open `http://127.0.0.1:8010/`.
2. Enter the bearer token from `env/.env.mcp`, or a reader key issued by
   `harborrag auth keys create` (server in `api_key` mode). In `api_key` mode
   the bearer token is accepted only when `HARBORRAG_MCP_READER_TENANT_ID`
   names one tenant.
3. Enter a tenant ID and select **Load tools**. Leave it blank to use the
   tenant bound to the token: `HARBORRAG_MCP_READER_TENANT_ID` for the owner
   token, or the key's own tenant for a reader key, which cannot pick another.
4. Fill in the arguments and select **Run tool**.

The page loads that tenant's effective catalog and builds argument controls
from each tool's JSON schema. A tool run goes through the same configuration,
policy, access and audit checks as the MCP transport. Results are shown as
formatted text, never injected as HTML, and retrieved content appears only
after an authenticated caller runs a tool.

### Configure tools from the browser

1. Open `http://127.0.0.1:8010/`.
2. Enter the owner token, the value of `HARBORRAG_MCP_BEARER_TOKEN`.
3. Select **Load**.

The editor shows the validated JSON form of `config/mcp/server.yaml`. **Save**
writes it back as YAML in one atomic step; **Reload YAML** discards unsaved
changes and reloads the file. What the file controls is in
[Tool configuration](#tool-configuration).

## Tenant-bound reader keys and shared corpus access

For anything beyond one developer's loopback session, do not share the local
bearer token. Give each person or workload its own **reader key**: a secret
bound to one tenant, with an expiry, that can list and run tools but cannot
change the server.

### Issue, list and revoke reader keys

1. Set `HARBORRAG_MCP_AUTH_MODE=api_key` in `env/.env.mcp` and restart the
   server.
2. Inside your repository checkout, create a key. `auth keys create` requires
   `--tenant`, `--owner` (`user-<name>`, `svc-<name>` or `team-<name>`),
   `--name` (the device or workload) and `--expires-in`:

   ```bash
   uv run harborrag auth keys create \
     --tenant engineering --owner user-alex --name alex-laptop --expires-in 7d
   ```

   The secret is written to `~/.harborrag/keys/alex-laptop.key`; the directory
   is created with mode `0700` when it is missing. `--secret-output` picks
   another new file, and `--migrate` applies pending control-plane migrations
   on a stack whose API has not started yet.

List a tenant's keys, revoke one key by its ID, or revoke every active key of
one owner in a tenant:

```bash
uv run harborrag auth keys list --tenant engineering
uv run harborrag auth keys revoke --key-id 0123456789abcdef01234567 --reason "laptop lost"
uv run harborrag auth keys revoke --tenant engineering --owner user-alex --reason "left team"
```

### How reader keys work

- A key looks like `hrk_<env>_v1_<key_id>.<secret>`. Only its SHA-256 hash is
  stored, in the control database table `mcp_api_keys`, with the tenant, the
  owner, an expiry and who issued it. The secret is written once to the
  output file (created exclusively, mode `0600`) and never printed; deliver it
  through your secret-sharing tool and delete the file.
- Every lifetime is bounded: at least 1 hour, and at most 7 days when
  `HARBORRAG_ENV=dev`, 30 days in `staging` and 90 days in `prod`. On a dev
  stack, `--expires-in 30d` is refused and the CLI prints the rule.
- A key is bound to the environment it was issued in and refused elsewhere.
  `mcp.sh` starts the server with the API's `HARBORRAG_ENV` from `env/.env.api`
  for that reason.
- The server looks the key up on **every** request and caches nothing, so a
  key created or revoked by the CLI takes effect on the next call, on every
  replica, without a restart. If the database cannot be reached, the request
  is refused.
- The tenant comes from the stored key, never from the client: a call naming
  another `tenant_id` is refused. In the status UI a reader key can load and
  run its own tenant's tools; the configuration editor and API stay
  owner-only.
- In `api_key` mode the owner token from `env/.env.mcp` keeps working beside
  the keys for that owner-only part, but only when
  `HARBORRAG_MCP_READER_TENANT_ID` names one tenant. The token never expires
  and cannot be revoked, and a TLS proxy forwards remote callers to the
  loopback listener, so it is not allowed to read every tenant. If the
  variable is unset or `*`, the server starts with reader keys only and logs a
  `WARNING`; set `HARBORRAG_MCP_READER_TENANT_ID=<tenant>` and restart to get
  the configuration editor back. Leave `HARBORRAG_MCP_BEARER_TOKEN` empty on a
  deployment that must have no owner over HTTP.
- Creating and revoking keys is recorded in the `activity` table. Rotation is
  create a new key, switch the client, revoke the old one; nothing rotates
  keys automatically.

### Where the CLI stores keys

`harborrag auth keys` writes to the same control database as the API, with the
owner credentials. Like every HarborRAG command, it runs inside a repository
checkout: it derives the database address from `env/.env.database` and takes
`HARBORRAG_ENV` from `env/.env.api`, unless `HARBORRAG_CONTROL_DB_URL` or
`HARBORRAG_ENV` is exported. It warns when it would fall back to the local
SQLite default, which no MCP server reads.

The older file-backed key list, `config/mcp/keys.yaml` with hashes in
environment variables, still works with `HARBORRAG_MCP_KEY_STORE=file`, but new
deployments should not use it. The repository ignores that file; copy
`config/mcp/keys.example.yaml` to start one. The server validates the file
strictly at startup and reads it again whenever it changes:

- On a re-read, an entry it cannot use (for example, a new key whose hash
  variable this process was not started with) is skipped with a `WARNING`;
  the other keys keep working.
- If the file is missing, empty or cannot be parsed, every key is denied and an
  `ERROR` is logged until it parses again, so a revocation saved with a typo
  never leaves the revoked key working.

### Shared corpus access

`HARBORRAG_CORPUS_ACCESS_MODE=source_acl` is the default: a caller reads only
the documents whose current source and document permission snapshots allow it.
Set it to `tenant_shared` only for a tenant whose published corpus is
intentionally shared among all its readers, and name that tenant with
`HARBORRAG_CORPUS_SHARED_TENANT_ID`, for example `DEFAULT`. Other tenants keep
`source_acl`, even in the same process.

A reader key's tenant must match each tool call's `tenant_id`; a caller cannot
select another tenant. Both HTTP modes bind to loopback; put TLS and any remote
access at a reverse proxy.

The local `HARBORRAG_MCP_BEARER_TOKEN` authenticates a connection but does not
grant corpus access by itself. To use it on an intentionally shared corpus,
also set `HARBORRAG_MCP_READER_TENANT_ID`,
`HARBORRAG_CORPUS_ACCESS_MODE=tenant_shared` and
`HARBORRAG_CORPUS_SHARED_TENANT_ID` to the target tenant, then restart the MCP
server; a running server keeps its previous access policy.

`HARBORRAG_MCP_READER_TENANT_ID` limits what the local token reads, not what it
may administer: the owner keeps the configuration API and the browser editor,
and a request that names no tenant (a catalog load or a tool call) is bound to
that tenant instead of being refused. A tool call naming another tenant is
still rejected with `403`.

## Entity summaries and facets

A **source entity** is one item of a source with everything attached to it,
such as a Jira issue with its comments and attachments. The summary worker can
write a card, a short dossier, for each source entity; `find_entities` and
`facet.*` filters search those cards.

Background summaries need their own approval. For a shared `DEFAULT` corpus,
set `HARBORRAG_TENANT_ID=DEFAULT` and
`HARBORRAG_SUMMARY_PROCESSING_ALLOWED=true`, and change
`HARBORRAG_SUMMARY_PROCESSING_REVISION` whenever that approval changes. The
summarization switch, model and budget of
[graph build](../../configuration/graph-build-config.md#summarization) must
also be configured. Reader keys never authorize model calls. Deployments that
use `source_acl` keep their snapshot-based processing rules.

Two settings decide what a card is and whether it can be searched:

- `HARBORRAG_SUMMARY_ENTITY_CARD_MAX_WORDS` (default `60`) is the length limit
  of a source-entity card. A source entity is the first level that spans
  documents, so it is the one worth writing as a dossier rather than a
  navigation hint. Raising the limit regenerates source-entity cards and
  leaves every other level cached, because the limit is part of the summary
  policy fingerprint.
- `HARBORRAG_SUMMARY_ENTITY_INDEX_ENABLED` (default `false`) publishes each
  accepted card as its own searchable vector point, so a question about a
  whole issue reaches the issue rather than one chunk of it. It needs a vector
  store reachable from the summary worker and costs one embedding per entity
  per regeneration. Set it to the same value for the worker
  (`env/.env.temporal`) and the MCP server (`env/.env.mcp`): on the MCP server
  alone, `find_entities` searches an empty index. Nothing is served from the
  point itself: a hit yields a node key, and the summary authority checks that
  binding's permissions and freshness again before releasing any evidence.

A card also reports its `coverage_mode`. `partial` means the connector found
source items, such as an attachment still in OCR or one nothing can parse, that
had no published version when the card was written; the card says how many.
Treat a `partial` dossier as provisional: it is replaced automatically once the
missing document lands.

### Facets: the filterable part of a card

A source scope can declare **facets**: named facts every entity card in that
scope carries. Each facet copies a structured field the connector already
extracted, verbatim and with no model call. Declare them per source in
`config/graph/build.yaml`, at most 12 per source. `field` takes a standard
issue attribute such as `status`, or a custom field by display name or
`customfield_NNNNN` ID; `type: integer` stores the value as a number so range
filters work:

```yaml
tenants:
  - tenant_id: DEFAULT
    sources:
      - source_scope_id: jira-main
        facets:
          - name: stage
            field: status
          - name: component
            field: "Component"
          - name: story_points
            field: "Story Points"
            type: integer
```

An entity whose issue does not set a declared field has no value for that
facet. Facets are part of the summary policy fingerprint, so editing them
regenerates that scope's source-entity cards and nothing else.

With `HARBORRAG_SUMMARY_ENTITY_INDEX_ENABLED=true`, facets are also written to
the entity point's payload under `facet.<name>` and indexed, so they filter:

- `vector_search` and the runtime retrieval service (the SDK) accept `facet.*`
  keys in `filters`, for
  example `{"facet.stage": "Done", "facet.component": ["billing", "payments"]}`.
  Such a filter selects entities on the entity lane and is taken out of the
  chunk filter, so the semantic lanes stay on instead of falling back to flat
  search.
- `find_entities` ranks whole entities against a question within a facet
  selection and returns each entity's summary, facets and released evidence
  IDs. It is the tool for "which issues are about this feature"; `fetch_evidence`
  on the returned IDs is how the answer gets cited.

## Filter search results

`vector_search`, the runtime retrieval service (the SDK) and chat accept
filters that narrow the search to matching chunks.

### Filter evidence by Jira fields

Facets select whole entities and need the summary pipeline. Evidence chunks can
be filtered directly too: every chunk of a Jira issue document (its summary,
description and comments) carries the issue's typed custom fields under
`fields.<key>`.

- An attachment, such as a design document, is a document of its own and does not copy
  the fields. A `fields.*` filter is first resolved to the matching issues, then the
  search covers those issues' evidence *and* the evidence of every document attached to
  them. Editing a field in Jira therefore takes effect as soon as the issue is ingested
  again, without reprocessing its attachments.
- The key is the field's display name in snake case (`Story Points` ->
  `story_points`, `Budget ($)` -> `budget`). A name that collides with
  another or with a reserved runtime key falls back to the field ID
  (`fields.customfield_10042`).
- Free-text custom fields are left out; they are searchable as evidence
  instead.

Values keep their Jira type and match exactly, case included. A scalar is an
equality, a list is "any of", and a mapping of bounds is a numeric range:

```json
{
  "fields.component": "Billing",
  "fields.team": ["Platform", "Payments"],
  "fields.story_points": {"gte": 3}
}
```

A filter matching more than 10,000 issues is rejected with a request to narrow
it. A `fields.*` filter cannot be combined with other `should` alternatives,
and it switches off entity-lane enrichment for that request, so no chunk of a
non-matching issue is fused back in.

The field map and the attachment-to-issue link are written at ingestion, so
documents ingested earlier gain them when they are next reprocessed.

### Filter keys for `vector_search`

`vector_search` and the runtime retrieval service accept three families of keys
in `filters`: the indexed payload keys below, `fields.<key>` and
`facet.<name>`. Any other key is rejected with
`unsupported evidence filter key(s)`. `tenant_id` is never a filter key; the
tenant is the request's own `tenant_id` field.

| Group | Keys |
| --- | --- |
| Document and chunk | `document_id`, `document_version_id`, `record_kind`, `chunk_kind`, `document_kind`, `language`, `relative_path` |
| Connector and source item | `connector_type`, `source_scope_id`, `source_item_id`, `parent_source_item_id`, `attachment_id`, `graph_source_node_key` |
| Connector container | `space_id`, `page_id`, `project_id`, `project_key`, `issue_key` |
| Source descriptor | `status`, `status_category`, `item_type`, `priority`, `assignee`, `reporter`, `labels`, `components`, `created_at`, `updated_at` |

Matching rules:

- A string matches exactly.
- A list matches any of its members; its items must be unique strings.
- `created_at` and `updated_at` take exact ISO-8601 strings, not ranges.
- Every indexed key above matches exactly. A range such as `{"gte": ...}` on
  one of them is rejected with `exact_match_only`.
- Range bounds (`gte`, `gt`, `lte`, `lt`) apply only to `fields.<key>` and
  `facet.<name>` fields stored as numbers.

```json
{
  "source_scope_id": "jira-main",
  "status_category": ["To Do", "In Progress"],
  "updated_at": "2026-10-01T00:00:00Z"
}
```

Keys combine with AND. HarborRAG builds only equality, any-of and range conditions from
`filters`, so a filter cannot express "not equal" or "field exists". The list of indexed
keys is `EVIDENCE_PAYLOAD_INDEXES` in
`packages/harborrag-engine/src/harborrag_engine/ingestion/projections/vector/vector_store.py`.

## Explorer MCP UI server

The **HarborRAG Explorer** is an
[MCP App](https://modelcontextprotocol.io/extensions/apps/overview): a tool whose
result is an interactive UI that the client renders in a sandboxed frame. It is
built with FastMCP 4 `FastMCPApp` and [Prefab](https://prefab.prefect.io/docs/welcome)
components, and it runs as its own MCP server, `harborrag-mcp-ui`.

The Explorer is optional. The reader server keeps its eleven tools and never
imports it, its image does not contain it, and `mcp.sh` never starts it. Add
the Explorer to a client as a second server when people want to search the
corpus themselves. Its dependencies, FastMCP Apps and Prefab, come from the
`ui` extra; without that extra `harborrag-mcp-ui` exits with an install hint.

| Tool | Visible to | Purpose |
| --- | --- | --- |
| `open_explorer` | Model | Opens the UI; `query` and `mode` pre-run a search, `tenant_id` preselects a tenant |
| `explorer_search` | App only | Evidence search or entity ranking |
| `explorer_read` | App only | One evidence chunk with its reading window, or a window of a document |
| `explorer_browse` | App only | Pages of documents or sources |
| `explorer_graph` | App only | Resolves a graph node, or expands the neighbourhood around one |

The UI has these tabs:

- **Search** runs `vector_search` or `find_entities`. Select a hit or an
  entity to open its evidence in the Reader. **Ask the assistant about these
  results** sends the top hits, with evidence IDs, to the chat.
- **Reader** shows the full evidence, its section, and the surrounding chunks
  from `get_document_context`. **Send to chat** posts the evidence as a message
  that cites its evidence ID. **Add to conversation context** gives it to the
  model without starting a reply. **Continue reading** pages through the
  document.
- **Documents** and **Sources** page through `list_documents` and
  `list_sources`. Select a document to read it.
- **Graph** resolves a title, provider ID or node key with
  `resolve_graph_nodes`, then draws the `graph_subgraph_search` neighbourhood
  as a diagram. Select a node to expand around it.
- **Tools** lists the reader catalog.

Every Explorer tool calls the same handlers the reader server registers, with
the same configuration file, tenant binding, budgets, output-schema checks and
audit trail. A tab exists only while its reader tool is enabled, and the server
refuses to start when none is.

### Run the Explorer

Run it like the reader server: it accepts the same flags and environment, and
uses the same bearer token or reader keys. Over HTTP it listens on port `8011`
by default, set by `HARBORRAG_MCP_UI_PORT`, and serves only `/healthz` besides
the MCP endpoint. The status page and configuration API stay on the reader
server.

From a checkout, `scripts/deployment/mcp-ui.sh` runs it in Docker the way
`mcp.sh` runs the reader server:

| Command | Behavior |
| --- | --- |
| `mcp-ui.sh [OPTION...]` | Stdio server for an MCP client; options go to `harborrag-mcp-ui` |
| `mcp-ui.sh --check` | One-off handshake that prints the Explorer tools |
| `mcp-ui.sh --http` | Starts the HTTP server in the background and waits for its health check |
| `mcp-ui.sh down` / `mcp-ui.sh logs` | Stops, or shows the logs of, the background HTTP server |
| `mcp-ui.sh --build ...` | Rebuilds the reader image and the Explorer layer first |

The container comes from `deploy/compose/docker-compose.mcp-ui.yml`. It extends
the reader service, so it gets the same env files, host networking, database
role, object-store credentials and read-only `config/` mount. Its image,
`deploy/docker/Dockerfile.mcp-ui`, is a thin layer over the reader image that
adds only the locked `ui` extra. Its audit trail lives in its own
`harborrag-mcp-ui-audit` volume.

To run it without Docker from a checkout, start the data services first, then:

```bash
uv sync --all-packages --extra ui
uv run harborrag-mcp-ui --check --local-stack-root .
uv run harborrag-mcp-ui --http --local-stack-root .
```

On a server where the package is installed:

```bash
pip install 'harborrag-mcp-server[reader,ui]'
harborrag-mcp-ui --http --env-file /etc/harborrag/reader.env --config /etc/harborrag/mcp.yaml
```

For a stdio client that renders MCP Apps, register both servers:

```json
{
  "mcpServers": {
    "harborrag": {"command": "harborrag-mcp", "args": ["--env-file", "/etc/harborrag/reader.env"]},
    "harborrag-explorer": {"command": "harborrag-mcp-ui", "args": ["--env-file", "/etc/harborrag/reader.env"]}
  }
}
```

Clients without MCP Apps support get a short text result from
`open_explorer`. The renderer script loads from `cdn.jsdelivr.net`, which the
tool declares in its resource CSP, so the client must allow that origin.

### Inspect with the MCP Inspector

The [MCP Inspector web client](https://modelcontextprotocol.io/docs/2026-07-28/tools/inspector/web)
shows every tool, the JSON-RPC traffic, and an **Apps** tab that renders the
Explorer. Start the Explorer over HTTP, then launch the Inspector against it:

```bash
scripts/deployment/mcp-ui.sh --http
npx @modelcontextprotocol/inspector --server-url http://127.0.0.1:8011/mcp
```

Open the URL the Inspector prints, because it carries the Inspector's
per-launch session token. In the server's **Server Settings**, add the header
`Authorization: Bearer <HARBORRAG_MCP_BEARER_TOKEN>` and connect. Then open
**Apps** and select `open_explorer`. Point the Inspector at port `8010` to
inspect the reader tools instead.

The Apps sandbox is served over plain `http`. Browse the Inspector at
`localhost` or `127.0.0.1`, not at `[::1]`, or the frame is blocked.

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
| `GET /api/config` | Owner | Source and effective settings, revision, active environment overrides, restart state |
| `PUT /api/config` | Owner | Saves a configuration; takes `configuration` and `expected_revision` |
| `POST /api/config/reload` | Owner | Reloads and validates the YAML file |
| `GET /api/tools?tenant_id=<tenant>` | Reader or owner | The tenant's effective tool catalog |
| `POST /api/tools/call` | Reader or owner | Runs a named tool with an `arguments` object |

A reader key reaches only its own tenant through the two tool endpoints.

## Use from Python

An application or test can run the server in process. The module-level
`list_tools()` returns the tool catalog as plain dictionaries, with no backend
connection:

```python
from harborrag_mcp_server.server import list_tools

for tool in list_tools():
    print(tool["name"])
```

To call tools, build the server on a reader application. It reads the same
`HARBORRAG_*` settings as the server, so the database, object store, vector
store and model variables must be set:

```python
import asyncio

from harborrag_mcp_server.server import McpServer
from harborrag_runtime.composition.readers import open_reader_application
from harborrag_runtime.config.settings import RuntimeSettings


async def main() -> None:
    application = open_reader_application(RuntimeSettings())
    server = McpServer(invoker=application.invoker, references=application.references)
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

## Container image

Build and check the MCP image from the repository root:

```bash
docker build -f deploy/docker/Dockerfile.mcp -t harborrag-mcp .
docker run --rm harborrag-mcp --check
```

The image contains the runtime with the reader extras, the packaged prompt
templates and the repository's `config/` folder, including
`config/models/models.yaml` and `config/mcp/server.yaml`. It runs as the
non-root `harborrag` user, keeps the audit log under that user's home
directory, and uses stdio by default.

An MCP client that starts the container must keep stdin open with `-i` and
provide the protected model and database environment plus reachable data
service addresses.

The checked-in HTTP launcher is loopback-only on purpose. Run
`scripts/deployment/mcp.sh --http` on the host for the status page and
configuration editor; do not publish an unauthenticated or remotely bound
development MCP endpoint from a container.

## Security boundaries

- **No secrets key.** The server is not given
  `HARBORRAG_SECRETS_ENCRYPTION_KEY`, so it cannot decrypt stored connector
  credentials.
- **Loopback only.** The launcher refuses to bind anything but `127.0.0.1`,
  `localhost` or `::1`. Static tokens are for local development only.
- **Local stdio is the one unauthenticated path.** It opens no listener and
  must be selected explicitly. Every other transport fails closed without an
  authentication provider.
- **Remote or production HTTP requires** TLS, a production JWT/JWKS token
  verifier, and tenant and capability authorization for every service-backed
  tool.
- **Retrieved source text and chat input are untrusted data.** Tool
  descriptions and stored prompt templates contain only developer-written
  instructions.

Every call passes a capability check, JSON-schema validation, argument budgets,
result and output budgets, and an owner-only JSONL audit at
`.harborrag/mcp-audit.jsonl` (change it with `HARBORRAG_MCP_AUDIT_PATH`). Audit
records hold a principal identifier, a digest of the arguments and the
outcome, never the bearer token or raw arguments. See
[Policy and audit status](README.md#policy-and-audit-status) for the full
sequence.

## Next

- [MCP Tools](README.md): the eleven tools, their arguments, and what they
  return.
- [Extending HarborRAG](../../../developers/extending/README.md#application-and-mcp-surfaces):
  keep service tools in `harborrag-mcp-server` and call runtime or service interfaces
  rather than provider clients.
