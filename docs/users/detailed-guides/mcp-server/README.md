# MCP Tools

Look up the tools HarborRAG offers to AI assistants, IDEs and agents, what
each one takes and returns, and the order to call them in. The
[Model Context Protocol](https://modelcontextprotocol.io/) (MCP) is an open
standard that lets such a client discover and call tools on a server. The
HarborRAG MCP server, `harborrag-mcp` from the `harborrag-mcp-server` package,
exposes eleven read-only tools that search and read your indexed content. Every
call passes the same policy checks and is written to an audit log.

To start the server and connect a client first, see
[Setup and Integration](setup-and-integration.md).

## Tools

A few terms used below:

- A **tenant** is an isolated workspace; every tool except `describe_graph`
  needs its `tenant_id`, and a caller can read only the tenants its token
  allows.
- A **chunk** is one indexed passage of a document; its `chunk_id` is what
  you pass to `fetch_evidence` to read and cite it.
- **Evidence** is source text you may cite. Graph records only help you
  navigate between documents and entities.

The ranges below are the effective values of the shipped
`config/mcp/server.yaml`; a deployment can lower them per tool or per tenant.

| Tool | Required arguments | Optional arguments | Returns |
| --- | --- | --- | --- |
| `vector_search` | `query`, `tenant_id` | `top_k` (1–20, default 5), `lane` (`dense`/`sparse`/`hybrid`, default `hybrid`), `mode` (`flat`/`local_semantic`, default `flat`), `filters`, `observe_graph` (default false), `include_content` (default true), `score_threshold` (0.0–1.0) | Matching chunks, retrieval diagnostics and cost availability |
| `fetch_evidence` | `tenant_id`, `items[].chunk_id` (1–10 items) | Expected document and version IDs per item | Canonical text, a citation locator, and `available`/`unavailable`/`output_limit` per item |
| `get_document_context` | `tenant_id`, `document_id` | Expected version, one anchor (chunk or section path), `cursor`, `limit` (1–10), `include_outline` | `document_title`, ordered chunks, and a cursor bound to the tenant, principal and version |
| `list_sources` | `tenant_id` | Source IDs, connector types, `cursor`, `limit` (1–20) | Only the source scopes the caller may read, with freshness metadata; never connection configuration |
| `describe_graph` | None | None | `ok`, `versions`, `layers` and `properties` of the graph model |
| `graph_triplet_search` | `tenant_id`, plus at least one of `subject`, `predicate`, `object` | `limit` (1–20, default 10) | Active canonical subject–predicate–object records |
| `graph_subgraph_search` | `tenant_id`, `start_node` | Relation types, depth (1–4), nodes (1–20), direction | The permitted neighbourhood, at most 40 edges, with completion reasons |
| `graph_path_search` | `tenant_id`, `start_node`, `end_node` | Relation types, depth (1–4), paths (1–5), direction | Permitted bounded paths with stable relation provenance |
| `resolve_graph_nodes` | `tenant_id`, `selector` | Source IDs, entity types, `limit` (1–10) | Unique, ambiguous or no-match candidates |
| `list_documents` | `tenant_id` | `cursor`, `limit` (1–20) | Active readable documents with title, version, source and chunk count |
| `find_entities` | `tenant_id`, `query` | `facets`, `source_ids`, `limit` (1–20, default 10) | Ranked entities with summary, facets, and evidence chunk IDs for `fetch_evidence` |

What the catalog leaves out:

- Chat is **not** an MCP tool. It is the HarborRAG HTTP API at
  `/v1/chat/completions`; see [Chat](../../chat/README.md).
- Ingestion is controlled through the CLI or the authenticated HTTP API, never
  through MCP.
- A separate server, `harborrag-mcp-ui`, offers the interactive
  [Explorer](setup-and-integration.md#explorer-mcp-ui-server): a UI that helps
  people search, read and trace the corpus, rather than an evidence source for
  models.

The server also sends clients that show server-level `instructions` a short
guide to which tool fits which question.

## Defaults and limits

Each deployment sets tool defaults and limits in
[`config/mcp/server.yaml`](../../../../config/mcp/server.yaml); see
[Tool configuration](setup-and-integration.md#tool-configuration). The shipped
file sets `vector_search` `top_k` to 5 and leaves graph observation off. To see
the values a tenant actually gets, read `GET /api/tools?tenant_id=<tenant>` on
the server's HTTP transport.

- For compact discovery, call `vector_search` with `include_content: false`,
  then fetch the chunks you choose with `fetch_evidence`.
- `vector_search` reports `cost.status: unavailable` until embedding and
  reranking cost telemetry is available; an unknown charge is never reported
  as zero. Document, evidence and graph reads claim no model charges.
- Document listing checks permissions on at most 10,000 documents and loads
  content only for the requested page. A larger readable catalog fails
  explicitly instead of being shown as complete. Every metadata and content
  read checks publication and permissions again after reading.
- Cursors are scoped to the tenant and principal and live in process memory,
  so a server restart invalidates them. An agent's cursors stay valid for the
  lifetime of its tool provider, normally one run.

Over HTTP, tool calls accept `reader` and `owner` tokens that hold the tenant.
Changing the server's configuration needs the `owner` token; see
[Tenant-bound reader keys](setup-and-integration.md#tenant-bound-reader-keys-and-shared-corpus-access).

## Evidence and graph workflow

1. Call `list_sources` when you do not know what the corpus holds.
2. Call `vector_search` to find candidate chunks for a question, or
   `find_entities` when the question asks which issues, pages or other
   entities match. Set `vector_search` `mode` to `local_semantic` for bounded
   graph expansion.
3. Call `fetch_evidence` on the chunk IDs you want to cite. It checks
   publication, permissions and any expected document or version again, so it
   also verifies a citation.
4. Call `get_document_context` when you need the ordered text around a chunk,
   or the document title.

The graph tools navigate between documents and entities; they never replace
source evidence. Call `describe_graph` first if you do not know the graph model
yet, or if selectors, relations or directions are unclear.

Graph selectors take three exact forms:

- a `node_key`
- a `logical_id`
- an exact, complete `title`, matched case-insensitively and never partially;
  chunk nodes have no title

In practice, pass a `chunk_id` from `vector_search`, which is also its `Chunk`
node key, or call `resolve_graph_nodes` and pass a returned `node_key`.
Duplicate titles stay explicitly ambiguous.

`graph_triplet_search` also accepts `predicate` alone. A predicate is a
relation type, not a node, so you can list every relationship of one type
without holding a node.

## Graph contract discovery

`describe_graph({})` returns a static description of the graph model:

- `ok`: always `true`.
- `versions`: the structural, semantic and ontology schema versions.
- `layers`: the node kinds and projected relation types.
- `properties`: property names for common nodes, document-owned nodes,
  `Chunk`, `Entity` and `RELATES`.

It does not query a tenant or return live graph statistics. Semantic and
ontology properties describe the declared contract, not a promise that every
node or relation stores them. Selector rules, traversal limits and defaults
belong to each tool's input schema. The tool takes no arguments.

## `observe_graph` is diagnostics, not evidence

`vector_search` with `observe_graph: true` adds a shallow provenance
observation under the response's `diagnostics` field. It starts from up to ten
of the returned `chunk_id`s, walks two hops in both directions, and summarizes
the counts, documents and sections it touched.

It never loads the content of a newly found chunk and never ranks neighbouring
evidence. A graph failure leaves an empty observation instead of failing the
search. Treat it as context for the results you already have, not as a way to
retrieve more evidence for a model.

## Policy and audit status

`McpServer` records every call attempt and its outcome, validates each declared
JSON schema, and enforces argument, result-count and output-size budgets.
Every call passes these steps in order:

1. **Capability check**: all eleven tools declare `read`; nothing else is
   registered. The separate Explorer server calls the reader tools through
   this same pipeline.
2. **Schema validation**: the declared JSON schema, with
   `additionalProperties: false`.
3. **Argument budget**: a ceiling on the size of the serialized arguments.
4. **Tenant scope**: every tool except `describe_graph` requires `tenant_id`,
   and `filters` cannot carry a `tenant_id` to reach another tenant.
5. **Execution**: retrieval runs as the caller's principal, so source and
   document permissions apply.
6. **Result budgets**: ceilings on the result count and the serialized output.
7. **Audit**: an owner-only JSONL record of the principal, a *digest* of the
   arguments, and the outcome. Raw query text and bearer tokens are never
   written.

Network transports must use an authentication provider. The only exception is
local stdio, which opens no network listener and which the caller selects
explicitly.

## Call the tools from Python

An application or test can run the same server in process; see
[Use from Python](setup-and-integration.md#use-from-python).

## Next

- [Setup and Integration](setup-and-integration.md): start the server, connect
  Claude Desktop, Cursor or VS Code, issue reader keys, configure the tools.
- [Chat](../../chat/README.md): the HTTP chat API.
- [Troubleshooting](../../troubleshooting/README.md): when a client cannot
  connect.
