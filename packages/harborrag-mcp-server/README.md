# harborrag-mcp-server

The HarborRAG MCP server. It gives MCP clients, such as Claude Desktop, Cursor or VS Code,
eleven read-only tools that search and read your indexed content, and checks and audits
every call. An MCP client is the application that calls the tools.

## Install

Run the server from a HarborRAG repository checkout whose data services are running and
hold ingested content. The
[Quick Start](https://github.com/cbtw-apac/HarborRAG/blob/main/docs/getting-started/quick-start.md)
sets that up, and its install already provides the `harborrag-mcp` command. The
checkout's wrapper, `scripts/deployment/mcp.sh`, runs the server in Docker with the
checkout's `env/` files.

The server needs only the `reader` extra. It connects the configured database, object
store, vector store, graph store and embedding model; it starts no chat model and no
ingestion, and creates no object-store buckets. For an environment that holds only the
server, sync that profile alone; `uv sync` removes every package the profile does not
need, the `harborrag` CLI included:

```bash
uv sync --package harborrag-mcp-server --extra reader
```

## Quick start

Check that the server starts and lists its tools, without connecting to any provider:

```bash
uv run harborrag-mcp --check
```

You should see the eleven tool names:

```text
["vector_search", "fetch_evidence", "get_document_context", "list_sources", "describe_graph", "graph_triplet_search", "graph_subgraph_search", "graph_path_search", "resolve_graph_nodes", "list_documents", "find_entities"]
```

`scripts/deployment/mcp.sh --check` runs the same check in the container. To connect a
client, follow
[MCP Setup and Integration](https://github.com/cbtw-apac/HarborRAG/blob/main/docs/users/detailed-guides/mcp-server/setup-and-integration.md#connect-an-mcp-client).

### Docker Desktop on Windows

The wrapper's container reaches the data services on the host through `127.0.0.1`, so it
uses the host network. On Docker Desktop, turn on host networking as described in
Docker's [host network driver](https://docs.docker.com/engine/network/drivers/host/#docker-desktop)
documentation; without it the container cannot reach Qdrant or the other data services,
and the client reports the HarborRAG server as disconnected.

Keep `HARBORRAG_QDRANT_PREFER_GRPC=false` in `env/.env.mcp`, as the template
`env-example/.env.mcp.example` sets it: Docker Desktop's host networking on Windows does
not carry Qdrant's gRPC port, so the client must use HTTP. Restart your MCP client after
changing its MCP configuration or `env/.env.mcp`.

## What it provides

- **`harborrag-mcp`**: the server. It talks to the client over stdio by default, the
  client starting it as a child process, or over loopback HTTP with `--http`, which adds a
  status page, a Tool Playground and a configuration editor. The HTTP endpoint is
  `http://127.0.0.1:8010/mcp` by default; `--host`, `--port` and `--path` change it, and a
  host that is not loopback is refused. Both transports are covered in
  [MCP Setup and Integration](https://github.com/cbtw-apac/HarborRAG/blob/main/docs/users/detailed-guides/mcp-server/setup-and-integration.md).
- **`harborrag-mcp-ui`**: the optional HarborRAG Explorer, an MCP App with a search UI,
  served as a second server. It needs the `ui` extra; see
  [Explorer MCP UI server](https://github.com/cbtw-apac/HarborRAG/blob/main/docs/users/detailed-guides/mcp-server/setup-and-integration.md#explorer-mcp-ui-server).
- **The eleven reader tools**: search, evidence fetch and document context, source and
  document listing, graph description, bounded graph search and node resolution, and
  entity ranking. What each one takes and returns is in
  [MCP Tools](https://github.com/cbtw-apac/HarborRAG/blob/main/docs/users/detailed-guides/mcp-server/README.md).
  Chat is not an MCP tool; it is the HTTP API's `POST /v1/chat/completions`.
- **Tool configuration** in `config/mcp/server.yaml`: enabled tools, defaults and limits,
  globally and per tenant. Tenant names in the file may not carry surrounding whitespace.
  See
  [Tool configuration](https://github.com/cbtw-apac/HarborRAG/blob/main/docs/users/detailed-guides/mcp-server/setup-and-integration.md#tool-configuration).
- **A container image**, `deploy/docker/Dockerfile.mcp`; see
  [Container image](https://github.com/cbtw-apac/HarborRAG/blob/main/docs/users/detailed-guides/mcp-server/setup-and-integration.md#container-image).

## Security and audit

- Every call passes a capability check, schema validation, argument, result-count and
  output budgets, and a tenant check; the order is in
  [Policy and audit status](https://github.com/cbtw-apac/HarborRAG/blob/main/docs/users/detailed-guides/mcp-server/README.md#policy-and-audit-status).
  Tools that need the ingestion capability fail closed until it is enabled.
- A network transport fails closed without an authentication provider. Local stdio is
  the one unauthenticated path; it opens no listener and the caller selects it
  explicitly.
- Over HTTP, tool calls accept a `reader` or `owner` token whose `tenants` claim holds
  the requested tenant, or the wildcard `*`. Changing the server's configuration needs
  the owner. Reader keys are covered in
  [Tenant-bound reader keys](https://github.com/cbtw-apac/HarborRAG/blob/main/docs/users/detailed-guides/mcp-server/setup-and-integration.md#tenant-bound-reader-keys-and-shared-corpus-access).
- The transport audits every attempt and outcome to an owner-only JSONL file with a
  principal identifier and a digest of the arguments; raw arguments and tokens are never
  written. The runtime records each reader tool's execution outcome separately, in
  `~/.harborrag/tool-execution-audit.jsonl` by default (`HARBORRAG_TOOL_EXECUTION_AUDIT_PATH`
  chooses another path); the invocation identifier correlates the two.
- Tool descriptions never contain retrieved document text, and no tool exposes raw
  database or provider access.

## Contributing

The package owns the MCP transport, its configuration, policy and audit:

```text
__main__.py                 stdio and HTTP launcher, flag parsing
server/base.py              server protocol
server/server.py            tool registry, policy enforcement, dispatch
server/http.py              loopback Streamable HTTP transport and status UI routes
server/http_auth.py         bearer authentication; reader-or-owner and owner-only guards
server/http_responses.py    JSON error responses of the HTTP routes
server/api_keys.py          hashed, tenant-bound reader keys
server/static/status.html   status page and Tool Playground
server/explorer/            HarborRAG Explorer MCP App served by harborrag-mcp-ui
configuration/              config/mcp/server.yaml loading, validation, environment overrides
policy.py                   MCP's view of the compiled safety ceilings
audit.py                    JSONL audit writer
defaults/mcp.yaml           packaged fallback configuration
```

The reader tool implementations, schemas, catalog and checked dispatcher live in
`harborrag_engine.tools`, and the generic contracts in `harborrag_core`.
`harborrag_runtime.composition.readers` connects the reader backends and owns their
lifecycle. This package adapts authentication, registration, configuration and protocol
responses around them.

Tests for this package live in `packages/harborrag-mcp-server/tests/`. Keep new tests
there when you add or change behaviour this package owns, and run them from the
repository root:

```bash
uv run pytest packages/harborrag-mcp-server/tests
```

See [CONTRIBUTING](https://github.com/cbtw-apac/HarborRAG/blob/main/CONTRIBUTING.md) for the
contribution workflow.

## License

Apache-2.0. See [LICENSE](https://github.com/cbtw-apac/HarborRAG/blob/main/LICENSE).
