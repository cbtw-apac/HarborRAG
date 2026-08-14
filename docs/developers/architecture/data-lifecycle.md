# Data Lifecycle

HarborRAG turns a changing source item into an authoritative, versioned knowledge artifact,
then serves evidence only from the version that is currently active. The lifecycle is
designed around one rule: indexes accelerate retrieval, but they never decide what is true.

## Storage ownership

| Store | Owns | Does not own |
| --- | --- | --- |
| PostgreSQL | Tenants, sources, documents, versions, jobs, active-version pointers, and publication state | Parsed content or search ranking |
| S3-compatible object store | Immutable raw, parsed, canonical, chunk, representation, and manifest artifacts | Publication authority |
| Qdrant | Dense and sparse vector projections addressed by tenant and document version | The active version of a document |
| FalkorDB | Deterministic document structure and source-declared relationships | Document authority or inferred facts |

Because the object-store artifacts are immutable and versioned, operators can inspect evidence
or rebuild projections without reconnecting to the original source. Reindex still requires the
necessary canonical artifacts and compatible model configuration to be retained.

## Ingestion path

Ingestion moves through four public phases. Implementations may split these phases into smaller
workflow activities, but the boundaries stay the same.

### 1. Discover and admit

The connector discovers lightweight `SourceRecord` values and describes a candidate before
loading its full content. Runtime checks tenant scope, source identity, policy, and whether the
source has changed. This early checkpoint avoids expensive parsing for items that should be
skipped.

### 2. Capture and understand

Accepted content is loaded into `RawDocument`, parsed into `ParsedDocument`, and normalized into
canonical `Document`, element, provenance, and relationship records. Parser routing uses metadata
and supported capabilities; fallback remains inside a parser family rather than silently treating
an incompatible format as generic text.

Raw, parsed, and canonical outputs are written as immutable artifacts. Normalization preserves
source identity, permissions, content type, timestamps, warnings, and addressable structure.

### 3. Chunk and project

Chunking is source-aware. It can produce compact route chunks for discovery and evidence chunks
that retain the content needed for grounded answers. Tables and structural elements remain
first-class records instead of being flattened without provenance.

Representations are encoded into dense, sparse, or both vector forms according to configuration.
Qdrant receives vector projections and FalkorDB receives deterministic structure or relationships
already declared by the source. Projection manifests bind outputs to a tenant, document version,
artifact set, and vector space.

### 4. Verify and publish

HarborRAG verifies required projections before changing the active-version pointer in PostgreSQL.
The publication update is atomic at the authority boundary. A failed projection therefore cannot
make a partial version visible to retrieval.

Older projections may be cleaned up later. Cleanup failure is operational debt to retry, not a
reason to pretend a distributed rollback occurred.

```text
discovered -> admitted -> artifacts written -> projections verified -> active
                    \-> failed (diagnosable and safe to retry)
```

## Retrieval path

Retrieval does not run through Temporal. A request follows a direct path:

1. An SDK, CLI, HTTP, or MCP boundary constructs the query and required access context.
2. The retrieval engine selects dense, sparse, or hybrid lanes and queries Qdrant.
3. Candidate `document_version_id` values are checked against PostgreSQL's active-version state.
4. Immediately before returning, the runtime revalidates loaded candidates against the active
   pointer so a publication that advances during evidence loading cannot expose a superseded
   version. Inactive, superseded, unauthorized, or malformed candidates are removed.
5. Evidence content and byte ranges are read from immutable artifacts.
6. Optional graph expansion adds bounded structural context from FalkorDB.
7. Results return normalized provenance and evidence, not raw provider responses.

The central visibility check is:

```text
candidate.document_version_id == authority.active_document_version_id
```

This protects readers during reindex, republish, delayed cleanup, and partial provider failure.

## Graph scope

The graph projection is deliberately deterministic. It represents document structure,
containment, chunk relationships, and relationships explicitly supplied by a source. Dedicated
graph retrieval can use those edges, and vector retrieval may observe graph context as a bounded
diagnostic or expansion step.

HarborRAG does not claim automatic named-entity resolution, LLM-based fact extraction, or a
general-purpose enterprise knowledge graph. Those can be added behind explicit contracts without
changing the authority model.

## Current boundaries

- MCP is a governed retrieval surface; it does not expose ingestion or chat.
- Sparse encoding must be configured intentionally; there is no silent automatic fallback.
- Query rewriting and reranking are extension points and are not guaranteed in every retrieval path.
- Cleanup and relationship repair can be eventually consistent after a safe publication.
- Production identity, TLS, network isolation, secret delivery, backups, observability, and capacity
  policy remain deployment responsibilities.

See [runtime reliability](runtime-reliability.md) for workflow behavior and the
[projection rebuild runbook](projection-rebuild.md) for repair procedures.
