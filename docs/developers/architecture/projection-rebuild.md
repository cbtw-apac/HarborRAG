# Projection rebuild after the architecture clean break

The `2.0.0a1` alpha release uses an internal vector contract that is a
clean break: provider-independent code uses index records and index operations,
while Qdrant collection/point terminology stays inside the Qdrant adapter.

## Inspecting collection drift

Ingestion uses exactly one current Qdrant collection per tenant: the evidence
collection. Its physical name is directly readable, for example
`DEFAULT_evidence`. Tenant IDs must
use ASCII letters, digits, `.`, `_`, or `-`; no payload filter is required for
tenant isolation because each tenant still owns separate collections.

Inventory collections before rebuilding or removing any projection:

```bash
curl -s "$QDRANT_URL/collections" | python -m json.tool
```

Anything other than `<tenant>_evidence` is retired. In particular a
`<tenant>_routes` collection is residue from before the route lane was removed:
nothing provisions, writes, searches, or deletes it, and its points carry
`preview` / `content_reference` payload keys that the current schema rejects.

Before deleting a retired `routes` collection, confirm nothing still points at
it. Manifests written before the retirement retain a `route_point_ids` list:

```sql
SELECT count(*) FROM projection_manifests
WHERE jsonb_array_length(COALESCE(manifest::jsonb->'route_point_ids', '[]'::jsonb)) > 0;
```

A non-zero count is expected and is not a blocker — those entries are inert,
because the only code that consumed them has been removed. Most belong to
document versions that are still active and would never have been cleaned up
anyway, which is why `ProjectionManifest.route_point_ids` survives as a
read-compatibility field rather than being deleted outright.

PostgreSQL document/version rows and immutable canonical artifacts remain
authoritative. Do not delete them. Qdrant and FalkorDB are rebuildable projections.

Chunk text is stored only as `content` in the evidence payload. FalkorDB stores
no content preview. Each evidence `chunk_id` is also an exact-key `Chunk` node
linked to document structure by `SUPPORTS`, which enables the retrieval flow:
vector search, exact `chunk_id` graph seed, then bounded subgraph expansion.

## Entering the graph

The graph stores identifiers and topology, not named entities, so it has no free-text
entry point of its own. Selectors resolve on `node_key`, `logical_id`, or an exact
lowercased `title` — nothing partial, and `title` is null on every `Chunk` node. The
bridge that makes the graph reachable is that a vector payload's `chunk_id` *is* the
`Chunk` node key, so `vector_search` resolves selectors for the graph tools.

Two traversal defaults follow from the spine not being uniformly directed —
`(:Chunk)-[:SUPPORTS]->(:Structure)` points *into* it while
`(:DocumentVersion)-[:CONTAINS]->(:Structure)` points down it:

- Path and subgraph queries default to `both`. A directed default cannot walk from a
  chunk to its own document, which is the most common question asked of this graph.
- Graph observation traverses undirected at depth 2, which is exactly far enough to
  reach a chunk's `Structure` and then its `DocumentVersion`.

Version filtering happens after the store answers, so every graph read widens its
request before rejecting stale records; otherwise a neighborhood dominated by superseded
versions returns a short result that looks like a genuinely small one. `truncated` means
"more exists than you were given"; rejected counts are reported separately as
`stale_count` and `unpublished_count`.

## Store ownership

| Store | Owns | Never holds | Rebuildable |
| --- | --- | --- | --- |
| PostgreSQL | Document/version state, atomic publication, projection manifests, cleanup and reindex jobs | Chunk text, vectors | No — source of truth |
| Object store | Immutable replay artifacts: canonical document, canonical chunks (the full `ChunkRecord`, including `embedding_text`, `search_text`, and `security`), canonical relations, chunk representations, vector and graph projections | Runtime fields, rejected by `reject_runtime_fields` | No — enables connector-free reindex |
| Qdrant | The single owner of *serving* chunk text (`content`), the dense and sparse retrieval vectors, and the minimum identity, citation, and filter metadata needed to rank and cite | Anything not needed to rank, filter, or cite | Yes |
| FalkorDB | Identifiers, topology, and provenance: the tenant spine and its structural edges | Any chunk text, preview, body, or credential | Yes |

When adding a field, place it by what it is for. Needed to **rank, filter, or
cite** a chunk, it belongs in the Qdrant payload. A **relationship between
identifiers**, it belongs in FalkorDB. Neither, it stays in the object-store
artifact only.

## The tenant spine

Every graph write descends from the tenant, and each connector adapts the middle
of the chain to its own hierarchy:

```text
(:Tenant)-[:HAS_DATA_SOURCE]->(:DataSource)-[:CONTAINS]->(:SourceEntity)
    -[:HAS_VERSION]->(:DocumentVersion)-[:CONTAINS]->(:Structure)<-[:SUPPORTS]-(:Chunk)
```

| Connector | `DataSource` expands to |
| --- | --- |
| Confluence | space → page (`PARENT_OF` for nesting) → attachment |
| Jira | project → issue (`PARENT_OF` for sub-issues) |
| GitHub | owner → repository → directory → file, plus ref `POINTS_TO` commit |
| SharePoint | site → drive → folder → file |
| Local | root → directory → file |

Tenant isolation is enforced in two independent places, and both are required.
Qdrant gives each tenant a physically separate collection, so `tenant_id` is
deliberately not a payload field. FalkorDB shares one graph, so `tenant_id` is
part of the node merge identity and of the uniqueness constraint, not merely a
filter property — version-owned node keys (`DocumentVersion`, `Structure`,
`Chunk`) do not hash the tenant, so without it two tenants that produced the same
document version would share a node.

## Why chunk text lives in two places

`content` appears in both the `canonical-chunks` artifact and the Qdrant payload,
and that is not redundancy to remove. They are different roles:

- **Object store — the rebuild input.** `canonical-chunks` holds the complete
  `ChunkRecord`, which is what re-chunking, representation reuse, relation repair,
  and connector-free reindex all read. Qdrant is declared rebuildable *from* it.
- **Qdrant — the serving copy.** Storing `content` beside the vector avoids an
  object-store fetch per search result.

Qdrant cannot replace the artifact, because the text that is actually embedded is
not `content`. With `contextualize_embeddings` enabled (the default), an evidence
chunk's `embedding_text` equals its `search_text` — content prefixed with document
and section context — and neither string is written to Qdrant. Reconstructing the
embedding input from the payload is therefore impossible, so a rebuild sourced from
Qdrant would silently produce different vectors.

The same asymmetry rules out storing `embedding_text` *as* `content`: `content` is
what gets cited and shown to the model, while `embedding_text` is a retrieval-only
representation carrying duplicated context headers.

There is no intra-tenant permission model. Everyone in a tenant sees everything,
which is why `security.permission_set_id` and `visibility` are carried on
`ChunkRecord` and persisted to the object store but deliberately not projected
into either store. `AuthoritativeProjectionSearch` validates version activeness,
not access.

For an existing development deployment:

1. Stop API and workers with `scripts/deployment/dev.sh down`.
2. Back up PostgreSQL and the configured object store.
3. Deploy the updated `2.0.0a1` packages and start data, Temporal, API, and workers.
4. Submit the existing connector-free reindex workflow for active documents.
5. Verify every new projection manifest before publication.
6. Drain version-addressed cleanup jobs only after the new versions are active.

Cleanup is asynchronous and retryable; it is not a distributed rollback. A failed
projection write must never change the active PostgreSQL document version.
