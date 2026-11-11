# Data Lifecycle

This page explains how HarborRAG turns a changing source item into a versioned,
authoritative record, and why retrieval returns evidence only from the version that is
currently active. It is for contributors who change ingestion or retrieval code. The terms
*tenant*, *canonical artifact*, *projection* and *active version* are defined in
[Architecture](README.md#terms-used-on-these-pages).

One rule shapes the whole lifecycle: indexes speed up retrieval, but they never decide what
is true.

## Storage ownership

| Store | Owns | Does not own |
| --- | --- | --- |
| PostgreSQL | Tenants, sources, documents, versions, jobs, active-version pointers and publication state | Parsed content or search ranking |
| S3-compatible object store | Immutable raw, parsed, canonical, chunk, representation and manifest artifacts | Publication authority |
| Qdrant | Dense and sparse vectors, with the chunk text and metadata needed to rank and cite, addressed by tenant and document version | The active version of a document |
| FalkorDB | Document structure, source-declared relationships and, when enabled, semantic topology | Document authority or chunk text |

Because the object-store artifacts are immutable and versioned, operators can inspect
evidence or rebuild projections without reconnecting to the original source. A rebuild still
needs the canonical artifacts and a compatible model configuration. Field-level ownership is
in [Projection Rebuild](projection-rebuild.md#store-ownership).

## Ingestion path

Ingestion moves through four phases. An executor may split them into smaller steps, such as
Temporal [activities](https://docs.temporal.io/activities), but the boundaries stay the same.

### 1. Discover and admit

The connector discovers lightweight `SourceRecord` values and describes each candidate before
loading its full content. Runtime checks tenant scope, source identity, policy, and whether
the source item has changed. This early checkpoint avoids expensive parsing for items that
should be skipped.

### 2. Capture and understand

Accepted content is loaded into a `RawDocument`, parsed into a `ParsedDocument`, and
normalized into a canonical `Document` with element, provenance and relationship records.
Parser routing uses metadata and declared capabilities; fallback stays inside one parser
family instead of silently treating an incompatible format as generic text.

Raw, parsed and canonical outputs are written as immutable artifacts. Normalization keeps
source identity, permissions, content type, timestamps, warnings and addressable structure.

### 3. Chunk and project

Chunking is source-aware. It writes evidence chunks, which keep the content needed for
grounded answers, and route records that outline a document and its sections. Route records
stay in the canonical chunk artifact and are never projected. Tables and structural elements
remain first-class records instead of being flattened without provenance. See
[Chunking Configuration](../../users/configuration/chunking-config.md).

Each evidence chunk is encoded twice: a dense vector from the embedding model and a
deterministic BM25 sparse vector, so the dense, sparse and hybrid retrieval lanes can all use
it. Qdrant receives the vectors with the chunk text and citation metadata. FalkorDB receives
the document structure and the relationships the source declares. Projection manifests bind
these outputs to a tenant, document version, artifact set and vector space.

### 4. Verify and publish

HarborRAG verifies the required projections before it changes the active-version pointer in
PostgreSQL. The publication update is atomic at the authority boundary, so a failed
projection cannot make a partial version visible to retrieval.

Older projections are cleaned up later. A cleanup failure is operational debt to retry, not a
reason to pretend that a distributed rollback happened.

```text
discovered -> admitted -> artifacts written -> projections verified -> active
                    \-> failed (diagnosable and safe to retry)
```

## Retrieval path

Retrieval does not run through Temporal. A request comes from the Python SDK, an MCP tool,
or chat over HTTP or `harborrag chat`; neither the HTTP API nor the CLI has a retrieval
command or route of its own. Every request follows the same direct path:

1. The entry point builds the query and the required access context.
2. Under the default `source_acl` access mode, canonical document permissions produce an
   allowlist that constrains vector ranking.
3. The retrieval engine queries Qdrant in the dense, sparse or hybrid lane.
4. Each candidate's `document_version_id` is checked against the active-version state in
   PostgreSQL.
5. Just before returning, runtime checks the loaded candidates against the active pointer
   and the permissions again, so a publication that advances in the meantime cannot expose a
   superseded version. Inactive, superseded, unauthorized or malformed candidates are
   removed.
6. Each result's text and citation fields come from the vector payload. Chat can rerank the
   results at this point.
7. Optional graph observation adds bounded structural context from FalkorDB, as diagnostics
   rather than extra evidence.
8. Results return normalized provenance and evidence, never raw provider responses.

To read a chunk's full canonical text, the MCP `fetch_evidence` tool and document-context
reads load it from the immutable artifacts and authorize it again.

The central visibility check is:

```text
candidate.document_version_id == authority.active_document_version_id
```

It protects readers during reindex, republish, delayed cleanup and partial provider failure.

## Graph scope

The structural graph is deterministic. It holds document structure, containment, chunk
relationships, and relationships a source declares explicitly. Graph tools walk those edges,
and vector retrieval can observe graph context as a bounded diagnostic step.

Semantic topology enrichment is opt-in per tenant and per source scope. It asks an LLM to
extract entities and typed facts within a closed ontology, keeps the exact evidence for every
fact, and merges entities only through audited, reversible decisions; a matching name never
merges two entities by itself. The built-in defaults make no LLM calls. See
[Graph Build Configuration](../../users/configuration/graph-build-config.md).

HarborRAG does not build a general-purpose enterprise knowledge graph, and none of this
changes the authority model above.

## Current boundaries

- MCP is a governed, read-only retrieval surface; it exposes neither ingestion nor chat.
- Only chat reranks, and only when the `retrieval.rerank` runtime setting is on; the MCP
  `vector_search` tool does not. See
  [Rerank chat retrieval](../../users/configuration/runtime-settings.md#rerank-chat-retrieval).
- Query rewriting is an engine port, `QueryRewriter`, that no runtime path wires yet.
- Connectors do not synchronize permission snapshots, and 2.0.0 has no command that
  imports them (`harborrag topology indexing permissions-import` was removed); a tenant
  whose corpus is deliberately shared uses `tenant_shared` instead. See
  [MCP Setup and Integration](../../users/detailed-guides/mcp-server/setup-and-integration.md).
- Cleanup and relationship repair can be eventually consistent after a safe publication.
- Production identity, TLS, network isolation, secret delivery, backups, observability and
  capacity policy remain deployment responsibilities; see
  [Runtime Reliability](runtime-reliability.md#deployment-responsibilities).

## Related

- [Runtime Reliability](runtime-reliability.md): workflow behaviour, retries and failure
  outcomes.
- [Projection Rebuild](projection-rebuild.md): what each store holds and how to repair
  projections.
