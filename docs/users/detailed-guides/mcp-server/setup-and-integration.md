<a name="mcp-setup-and-integration" id="mcp-setup-and-integration"></a>

# Connect HarborRAG over MCP

Connect an assistant or IDE and check that it can read your indexed sources.
The MCP client is the assistant or IDE. The HarborRAG server gives it tools
such as `list_sources`, `vector_search`, and `fetch_evidence`.

## Before you start

- Follow the [Quick Start](../../../getting-started/quick-start.md) to clone
  HarborRAG, start the stack, and ingest a source. Leave the data services running.
- From the checkout root, grant the MCP server its own read-only Postgres and
  MinIO credentials. Run this after the stack starts and repeat it after upgrades.

  ```bash
  scripts/deployment/dev.sh mcp-access
  ```

The local stack's example configuration makes published content available to
readers in tenant `DEFAULT`. Use the tenant configured for your own corpus if
it differs. [Shared corpus access](server-operations.md#shared-corpus-access)
explains how to check what a reader can see.

## Choose a transport

| If you want to… | Choose | How it connects |
| --- | --- | --- |
| Connect a local assistant or IDE that launches a command | [stdio](#stdio-for-external-clients) | The client starts the wrapper using local pipes. No port or MCP token is needed. |
| Connect an HTTP-capable client on this machine or use the browser playground | [Streamable HTTP](#local-http-and-status-ui) | You start the server first. The client connects to the loopback MCP URL with a bearer token. |

## stdio for external clients

From the checkout root, run the launcher's built-in check. The first run may
build the Docker image. The check prints the available tools, including
`list_sources`, without connecting to the data services.

```bash
scripts/deployment/mcp.sh --check
```

### Connect an MCP client

Copy the absolute path to `scripts/deployment/mcp.sh` in your checkout, then
use the [setup instructions for your client](clients.md). The client launches
the wrapper when it needs tools. Use `--check` only for the terminal diagnostic.

## Local HTTP and status UI

Start the server from the checkout root.

```bash
scripts/deployment/mcp.sh --http
```

The wrapper prints the browser status page at `http://127.0.0.1:8010/` when
it is healthy. An MCP client uses `http://127.0.0.1:8010/mcp` with
`Authorization: Bearer <token>`. For a personal local connection, get the
owner token from `HARBORRAG_MCP_BEARER_TOKEN` in the ignored `env/.env.mcp`
file. Use a client prompt or environment-backed setting to supply it.

See [HTTP client examples](clients.md#connect-an-http-client) for setup,
[reader keys](server-operations.md#tenant-bound-reader-keys-and-shared-corpus-access)
for other readers, or [run a tool from the browser](server-operations.md#run-tools-from-the-browser).
To stop the HTTP server, run `scripts/deployment/mcp.sh down`.

## Verify your connection

1. Open your client's MCP tool list and find `harborrag`. The default catalog
   contains eleven tools, including `list_sources` and `vector_search`.
2. Ask the assistant to run HarborRAG's `list_sources` for tenant `DEFAULT`.
   A successful connection returns a `sources` list. Look for the source you
   ingested. An empty list means the tool answered, so check the tenant,
   publication, and [corpus access](server-operations.md#shared-corpus-access).
3. Ask a question about that source and request citations. If you ingested the
   HarborRAG docs, try “How do I ingest a local folder?” The assistant can use
   `vector_search` to find matches and `fetch_evidence` to read them.

If the client cannot list tools, try the [connection checks](clients.md#troubleshoot-a-connection).

## Next

| I want to… | Guide |
| --- | --- |
| Set up my specific app or IDE | [Connect an MCP client](clients.md) |
| Look up tool arguments and responses | [MCP tool reference](README.md) |
| Use filters, entity search, or the Explorer app | [Search with MCP and HarborRAG Explorer](search-and-explorer.md) |
| Diagnose startup, issue reader keys, or manage the container | [Operate the MCP server](server-operations.md) |
| Set tool limits, see the management API, or use Python | [Configure MCP tools](tool-configuration.md) |

<details markdown="1"><summary id="sections-moved-from-this-page">Sections moved from this page</summary>

### Client setup

- <a name="connect-an-http-client" id="connect-an-http-client"></a>[Connect an HTTP client](clients.md#connect-an-http-client)

### Server operations

- <a name="run-the-wrapper-and-forward-server-options" id="run-the-wrapper-and-forward-server-options"></a>[Run the wrapper and forward server options](server-operations.md#run-the-wrapper-and-forward-server-options)
- <a name="override-launcher-settings-with-environment-variables" id="override-launcher-settings-with-environment-variables"></a>[Override launcher settings with environment variables](server-operations.md#override-launcher-settings-with-environment-variables)
- <a name="checks-before-the-http-server-starts" id="checks-before-the-http-server-starts"></a>[Checks before the HTTP server starts](server-operations.md#checks-before-the-http-server-starts)
- <a name="local-http-endpoints" id="local-http-endpoints"></a>[Local HTTP endpoints](server-operations.md#local-http-endpoints)
- <a name="request-limits" id="request-limits"></a>[Request limits](server-operations.md#request-limits)
- <a name="run-tools-from-the-browser" id="run-tools-from-the-browser"></a>[Run tools from the browser](server-operations.md#run-tools-from-the-browser)
- <a name="configure-tools-from-the-browser" id="configure-tools-from-the-browser"></a>[Configure tools from the browser](server-operations.md#configure-tools-from-the-browser)
- <a name="tenant-bound-reader-keys-and-shared-corpus-access" id="tenant-bound-reader-keys-and-shared-corpus-access"></a>[Tenant-bound reader keys and shared corpus access](server-operations.md#tenant-bound-reader-keys-and-shared-corpus-access)
- <a name="issue-list-and-revoke-reader-keys" id="issue-list-and-revoke-reader-keys"></a>[Issue, list and revoke reader keys](server-operations.md#issue-list-and-revoke-reader-keys)
- <a name="how-reader-keys-work" id="how-reader-keys-work"></a>[How reader keys work](server-operations.md#how-reader-keys-work)
- <a name="where-the-cli-stores-keys" id="where-the-cli-stores-keys"></a>[Where the CLI stores keys](server-operations.md#where-the-cli-stores-keys)
- <a name="shared-corpus-access" id="shared-corpus-access"></a>[Shared corpus access](server-operations.md#shared-corpus-access)
- <a name="container-image" id="container-image"></a>[Container image](server-operations.md#container-image)
- <a name="read-only-database-and-object-store-accounts" id="read-only-database-and-object-store-accounts"></a>[Read-only database and object-store accounts](server-operations.md#read-only-database-and-object-store-accounts)
- <a name="create-or-refresh-the-accounts" id="create-or-refresh-the-accounts"></a>[Create or refresh the accounts](server-operations.md#create-or-refresh-the-accounts)
- <a name="security-boundaries" id="security-boundaries"></a>[Security boundaries](server-operations.md#security-boundaries)

### Search and Explorer

- <a name="entity-summaries-and-facets" id="entity-summaries-and-facets"></a>[Entity summaries and facets](search-and-explorer.md#entity-summaries-and-facets)
- <a name="facets-the-filterable-part-of-a-card" id="facets-the-filterable-part-of-a-card"></a>[Facets: the filterable part of a card](search-and-explorer.md#facets-the-filterable-part-of-a-card)
- <a name="filter-search-results" id="filter-search-results"></a>[Filter search results](search-and-explorer.md#filter-search-results)
- <a name="filter-evidence-by-jira-fields" id="filter-evidence-by-jira-fields"></a>[Filter evidence by Jira fields](search-and-explorer.md#filter-evidence-by-jira-fields)
- <a name="filter-keys-for-vector_search" id="filter-keys-for-vector_search"></a>[Filter keys for `vector_search`](search-and-explorer.md#filter-keys-for-vector_search)
- <a name="explorer-mcp-ui-server" id="explorer-mcp-ui-server"></a>[Explorer MCP UI server](search-and-explorer.md#explorer-mcp-ui-server)
- <a name="run-the-explorer" id="run-the-explorer"></a>[Run the Explorer](search-and-explorer.md#run-the-explorer)
- <a name="inspect-with-the-mcp-inspector" id="inspect-with-the-mcp-inspector"></a>[Inspect with the MCP Inspector](search-and-explorer.md#inspect-with-the-mcp-inspector)

### Tool settings and Python

- <a name="tool-configuration" id="tool-configuration"></a>[Tool configuration](tool-configuration.md#tool-configuration)
- <a name="per-tool-controls" id="per-tool-controls"></a>[Per-tool controls](tool-configuration.md#per-tool-controls)
- <a name="tenant-overrides" id="tenant-overrides"></a>[Tenant overrides](tool-configuration.md#tenant-overrides)
- <a name="environment-variable-overrides" id="environment-variable-overrides"></a>[Environment variable overrides](tool-configuration.md#environment-variable-overrides)
- <a name="when-changes-take-effect" id="when-changes-take-effect"></a>[When changes take effect](tool-configuration.md#when-changes-take-effect)
- <a name="validation" id="validation"></a>[Validation](tool-configuration.md#validation)
- <a name="management-api" id="management-api"></a>[Management API](tool-configuration.md#management-api)
- <a name="use-from-python" id="use-from-python"></a>[Use from Python](tool-configuration.md#use-from-python)

</details>
