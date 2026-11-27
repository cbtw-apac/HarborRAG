# Search with MCP and HarborRAG Explorer

Narrow a search to the evidence or entities you need and use the Explorer to
read the results. For a first client connection, use the
[MCP quick start](setup-and-integration.md).

- [Filter search results](#filter-search-results)
- [Search entity summaries and facets](#entity-summaries-and-facets)
- [Run the HarborRAG Explorer](#explorer-mcp-ui-server)

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
  another or with a reserved runtime key, or is longer than 128 characters in
  snake case, falls back to the field ID (`fields.customfield_10042`). An issue
  keeps at most 128 fields, the first in key order; the rest are left out with
  a warning in the worker log.
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
| Source descriptor | `status`, `status_category`, `item_type`, `priority`, `assignee`, `reporter`, `labels`, `components`, `created_at`, `last_modified` |

Matching rules:

- A string matches exactly.
- A list matches any of its members; its items must be unique strings.
- `created_at` and `last_modified` take exact ISO-8601 strings, not ranges.
- Every indexed key above matches exactly. A range such as `{"gte": ...}` on
  one of them is rejected with `exact_match_only`.
- Range bounds (`gte`, `gt`, `lte`, `lt`) apply only to `fields.<key>` and
  `facet.<name>` fields stored as numbers.

```json
{
  "source_scope_id": "jira-main",
  "status_category": ["To Do", "In Progress"],
  "last_modified": "2026-10-01T00:00:00Z"
}
```

Keys combine with AND. HarborRAG builds only equality, any-of and range conditions from
`filters`, so a filter cannot express "not equal" or "field exists". The list of indexed
keys is `EVIDENCE_PAYLOAD_INDEXES` in
`packages/harborrag-engine/src/harborrag_engine/ingestion/projections/vector/vector_store.py`.

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
uses the same owner token or reader keys. Over HTTP it listens on port `8011`
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
the reader service, so it gets the same env files, Docker network, read-only
database role and object-store user, and read-only `config/` mount. Its image,
`deploy/docker/Dockerfile.mcp-ui`, is a thin layer over the reader image that
adds only the locked `ui` extra. Its audit trail lives in its own
`harborrag-mcp-ui-audit` volume.

To run it without Docker from a checkout, start the stack and run
`scripts/deployment/dev.sh mcp-access` first. `--local-stack-root` then takes
the read-only accounts' passwords from `env/.env.database`, and never its
`HARBORRAG_CONTROL_DB_URL` line, which holds the host CLI's owner URL:

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
