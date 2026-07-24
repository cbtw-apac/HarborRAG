# harborrag-engine

Owns RAG orchestration using core contracts and adapter implementations.

## Folder ownership

```text
ingestion/base.py + ingestion/chunking/
retrieval/base.py + retrieval/evidence.py + retrieval/pipeline.py
ingestion/indexing/{config,schemas,diff,preparation,batching,embedding,service}.py
ingestion/indexing/vector/ + ingestion/indexing/graph/
graph/base.py + graph/mock.py
```

## Team deliverables

- Implement production ingestion pipeline using only connector/parser/model/repository interfaces.
- Extend the structure-first chunker with source-specific and AST-backed strategies.
- Implement retrieval pipeline with rewrite, vector search, graph expansion, fusion, reranking, and evidence building.

## Ingestion chunking

`harborrag_engine.ingestion.chunking` implements the deterministic chunking
stages used before the immutable chunk registry:

```text
normalized elements
  -> structure segmentation
  -> oversized-unit refinement
  -> compatible peer packing
  -> separate source/context metadata
  -> stable logical + revision identity
  -> canonical chunk records
  -> lightweight manifest validation
```

`minimum_tokens` is a preferred merge threshold, `target_tokens` is the soft
packing target, and `maximum_tokens` is a hard postcondition. Tables are split
by complete rows and repeat their header only as bounded context metadata;
canonical chunk content remains suitable for fingerprinting and source diffs.

`ChunkingRouter` only selects an engine-owned strategy. `ChunkingService`
executes that strategy, validates its output, assigns stable
`logical_chunk_id` and content-specific `chunk_revision_id` values, and returns
a `ChunkingResult` containing canonical core `ChunkRecord` values plus a
manifest of lightweight chunk references.

Construct the default service with a model-compatible `TokenCounter` and a
framework-neutral `TextRefiner`. `ChunkingService.chunk` is synchronous, pure,
and deterministic. For restartable ingestion, pass its result to
`ChunkPersistenceService`; that separate bridge writes immutable chunk bodies
before the validated manifest. The validated result is the input boundary for
vector and graph indexing.

The default service resolves installed Markdown, HTML, and JSON structure
splitters through `HarborChunk`; explicitly injected splitters always take
precedence. When an optional structure provider is unavailable or returns no
usable sections, ingestion falls back to normalized parser elements. A
`TextRefiner` remains explicit because enforcing `maximum_tokens` is a hard
correctness requirement rather than an optional structural enhancement.

## Incremental vector and graph indexing

`harborrag_engine.ingestion.indexing` consumes canonical chunk records and their manifest:

```text
active manifest + proposed manifest
  -> logical chunk diff
  -> embedding batches for NEW / CHANGED / REEMBED_REQUIRED
  -> deterministic staged vector + graph mutation plans
  -> independent vector + graph repository upserts
  -> exact read-after-write validation
  -> provider-independent IndexingResult
```

`IncrementalChunkDiffer` preserves unchanged vectors and records removed or
superseded chunk revisions for a later activation step. `VectorIndexService`
never deletes active points and never marks staged points active. Every staged
point includes its generation, canonical chunk identities, source location,
content hash, token count, and embedding-configuration fingerprint.

`GraphIndexService` projects `Artifact -> Revision -> Section -> Chunk`, chunk
ordering, and source relationships supported by explicit Jira, Confluence, or
document metadata. Chunk graph nodes contain a bounded deterministic context
capsule, never the unrestricted canonical body. Both stores use deterministic
generation-scoped identities, remain staged after validation, and can be
reconciled when only one side succeeds. Provider adapters remain responsible
for native mappings and structurally implement the narrow repository ports in
`harborrag_core.ports.indexing`; engine indexing does not branch on Qdrant,
FalkorDB, or an embedding provider.


## Package tests

Tests for this package live in:

```text
packages/harborrag-engine/tests/
└── ingestion/
    ├── unit/
    ├── integration/
    └── smoke/
```

Run from the repository root:

```bash
pytest packages/harborrag-engine/tests/ingestion/unit
pytest packages/harborrag-engine/tests/ingestion/integration -m integration
python packages/harborrag-engine/tests/ingestion/smoke/indexing.py
```

The vector integration test uses embedded Qdrant. The graph integration test
requires a live FalkorDB service and `HARBORRAG_FALKORDB_INTEGRATION=1`.
The standalone smoke check uses real embedding, Qdrant, and FalkorDB adapters;
see `tests/ingestion/smoke/README.md` before running it.
