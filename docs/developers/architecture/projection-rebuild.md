# Projection Rebuild

This page is for operators and contributors who inspect, repair or rebuild HarborRAG's search
projections. It lists what each store holds, which Qdrant collections and graph edges are
current, and how projections are rebuilt. The terms *projection*, *canonical artifact*,
*tenant* and *active version* are defined in
[Architecture](README.md#terms-used-on-these-pages).

PostgreSQL document and version rows and the immutable canonical artifacts in the object store
are authoritative: never delete them. Qdrant and FalkorDB hold projections, which can always be
rebuilt from them.

## Store ownership

| Store | Owns | Never holds | Rebuildable |
| --- | --- | --- | --- |
| PostgreSQL | Document and version state, atomic publication, projection manifests, cleanup and reindex jobs | Chunk text, vectors | No: the source of truth |
| Object store | Immutable replay artifacts: canonical document, canonical chunks (the full `ChunkRecord`, including `embedding_text`, `search_text` and `security`), canonical relations, chunk representations, vector and graph projections | Runtime fields, which `reject_runtime_fields` refuses | No: it enables reindex without the connector |
| Qdrant | The serving copy of chunk text (`content`), the dense and sparse retrieval vectors, and the minimum identity, citation and filter metadata needed to rank and cite | Anything not needed to rank, filter or cite | Yes |
| FalkorDB | Identifiers, topology and provenance: the tenant spine and its structural edges, plus the semantic topology projection when enrichment is enabled | Any chunk text, preview, body or credential | Yes |

When you add a field, place it by what it is for. If it is needed to **rank, filter or cite** a
chunk, it belongs in the Qdrant payload. If it is a **relationship between identifiers**, it
belongs in FalkorDB. Otherwise it stays in the object-store artifact only.

## The tenant spine

Every graph write descends from the tenant, and each connector adapts the middle of the chain
to its own hierarchy:

```text
(:Tenant)-[:HAS_DATA_SOURCE]->(:DataSource)-[:CONTAINS]->(:SourceEntity)
    -[:HAS_VERSION]->(:DocumentVersion)-[:CONTAINS]->(:Structure)-[:HAS_CHUNK]->(:Chunk)
```

| Connector | `DataSource` expands to |
| --- | --- |
| Confluence | space → page (`PARENT_OF` for nesting) → attachment |
| Jira | project → issue (`PARENT_OF` for sub-issues) |
| GitHub | owner → repository → directory → file, plus ref `POINTS_TO` commit |
| SharePoint | site → drive → folder → file |
| Local | root → directory → file |

Tenant isolation is enforced in two independent places, and both are required. Qdrant gives
each tenant physically separate collections, so `tenant_id` is deliberately not a payload
field. FalkorDB likewise gives each tenant its own graph, named after the same tenant
namespace. `tenant_id` also stays part of the node merge identity and of the uniqueness
constraint, not merely a filter property: version-owned node keys (`DocumentVersion`,
`Structure`, `Chunk`) do not hash the tenant, so without it two tenants that produced the same
document version would share a node.

## Qdrant collections

Each tenant's collections are named `{prefix}_{tenant}_{index}`. The prefix comes from
`HARBORRAG_STORAGE_NAMESPACE_PREFIX` (default `harborrag`), so the default tenant writes
`harborrag_DEFAULT_evidence`. See
[One tenant namespace in every store](../../users/configuration/workspace-mode.md#one-tenant-namespace-in-every-store).

List the collections before you rebuild or remove anything. On the local dev stack, Qdrant
listens on `http://127.0.0.1:6333`; elsewhere use your `HARBORRAG_QDRANT_URL`:

```bash
curl -s "http://127.0.0.1:6333/collections" | python -m json.tool
```

The output lists one entry per collection, such as `"name": "harborrag_DEFAULT_evidence"`. The
[Qdrant collections documentation](https://qdrant.tech/documentation/concepts/collections/)
covers the API itself. These logical indexes are current:

| Index | Written by |
| --- | --- |
| `evidence` | Ingestion: one point per evidence chunk |
| `memories` | Chat long-term memory |
| `contextual-v2-<fingerprint>`, `parent-v2-<fingerprint>` | Semantic topology derived vectors, built by the topology runtime |

2.0.0 has no command that previews or deletes the derived-vector and graph projections of
retired topology builds: `harborrag topology derive` and `harborrag topology cleanup` were
removed with the rest of `harborrag topology`.

A collection whose name ends in `_routes` is residue from before the route lane was removed.
Nothing provisions, writes, searches or deletes it, and its points carry `preview` and
`content_reference` payload keys that the current schema rejects. Before you delete it,
confirm that nothing still points at it: manifests written before the retirement keep a
`route_point_ids` list.

```sql
SELECT count(*) FROM projection_manifests
WHERE jsonb_array_length(COALESCE(manifest::jsonb->'route_point_ids', '[]'::jsonb)) > 0;
```

A non-zero count is expected and does not block the deletion: those entries are inert, because
the only code that read them has been removed. Most belong to document versions that are still
active and would never have been cleaned up anyway, which is why
`ProjectionManifest.route_point_ids` survives as a read-compatibility field.

## Why chunk text lives in two places

`content` appears in both the `canonical-chunks` artifact and the Qdrant payload. That is not
redundancy to remove; the two copies have different roles:

- **Object store: the rebuild input.** `canonical-chunks` holds the complete `ChunkRecord`,
  which re-chunking, representation reuse, relation repair and connector-free reindex all
  read. Qdrant is rebuildable *from* it.
- **Qdrant: the serving copy.** Storing `content` beside the vector avoids an object-store
  fetch for every search result.

Qdrant cannot replace the artifact, because the text that is embedded is not `content`. With
`contextualize_embeddings` enabled (the default), an evidence chunk's `embedding_text` equals
its `search_text`, which is the content prefixed with document and section context, and
neither string is written to Qdrant. The embedding input cannot be rebuilt from the payload, so
a rebuild sourced from Qdrant would silently produce different vectors.

The same asymmetry rules out storing `embedding_text` *as* `content`: `content` is what gets
cited and shown to the model, while `embedding_text` is a retrieval-only representation that
repeats context headers.

## Access control is not projected

`security.permission_set_id` and `visibility` stay on the `ChunkRecord` in the object store;
they are not projected as grants into Qdrant or FalkorDB. Under the default
`HARBORRAG_CORPUS_ACCESS_MODE=source_acl`, retrieval gets an allowlist of authorized documents
from the canonical permission repository before vector ranking, and checks it again before it
returns candidates. Missing or expired permission snapshots deny access.
`AuthoritativeProjectionSearch` handles version activeness; the surrounding retrieval service
applies access control.

Connectors do not synchronize permission snapshots yet, and 2.0.0 has no command that imports
them (`harborrag topology indexing permissions-import` was removed). Run a tenant whose corpus
is deliberately shared in `tenant_shared` mode; see
[MCP Setup and Integration](../../users/detailed-guides/mcp-server/setup-and-integration.md).

## Entering the graph

The structural graph stores identifiers and topology, so it has no free-text entry point of its
own. Graph-tool selectors accept a `node_key`, a source item ID such as an issue key, or an
exact full title, compared case-insensitively and never partially; `Chunk` nodes have no
title. What makes the graph reachable is that a vector payload's `chunk_id` *is* the `Chunk`
node key, so `vector_search` results supply selectors for the graph tools.

The spine points down, from parent to child:
`(:DocumentVersion)-[:CONTAINS]->(:Structure)-[:HAS_CHUNK]->(:Chunk)`. A walk from a chunk to
its document therefore runs against every edge, which sets two traversal defaults:

- Path and subgraph queries default to direction `both`. An `outgoing` default could not walk
  from a chunk to its own document, which is the most common question asked of this graph.
- Graph observation traverses undirected at depth 2, exactly far enough to reach a chunk's
  `Structure` and then its `DocumentVersion`.

Graphs written before 2.0.0a2 linked chunks with `(:Chunk)-[:SUPPORTS]->(:Structure)`. The
graph tools no longer accept `supports` as a relation filter: use `has_chunk`, which points
the other way.

Version filtering happens after the store answers, so every graph read widens its request
before it rejects stale records; otherwise a neighbourhood dominated by superseded versions
would return a short result that looks like a genuinely small one. `truncated` means "more
exists than you were given"; rejected records are counted separately as `stale_count` and
`unpublished_count`.

## Rebuild projections

This release has two ways to rebuild projections:

- **Re-ingest with force.** A forced run fetches, parses and re-projects every discovered
  record, even when its source descriptor looks unchanged. See
  [Ingestion modes](../../users/ingestion-modes.md) for the HTTP, CLI and SDK forms.
- **Reindex without the connector.** The `harborrag.reindex` Temporal workflow rebuilds
  projections from canonical artifacts. No CLI command or HTTP route starts it yet; the
  deployed ingestion smoke starts it through the runtime's Temporal client.

To move a deployment between versions, follow [Upgrading](../../users/upgrading.md). Stored
projections are not migrated between 2.0.0a1 and 2.0.0a2, so that upgrade is a reset followed
by a full re-ingest.

Cleanup is asynchronous and retryable; it is not a distributed rollback. A failed projection
write never changes the active document version in PostgreSQL.
