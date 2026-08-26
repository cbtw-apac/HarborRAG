# harborrag-engine

Owns RAG orchestration using core contracts and adapter implementations.

## Folder ownership

```text
ingestion/admission.py       # source change and version planning
ingestion/chunking/          # canonical route and evidence chunking
ingestion/representations/   # dense/sparse representation policy
ingestion/projections/       # Qdrant/Falkor projection construction and verification
retrieval/                   # authoritative retrieval, fusion, reranking, and evidence
agent/                       # bounded multi-hop model/tool orchestration
conversation/                # compatibility exports for the core memory port
```

## Extension boundaries

- Core domain records and ports remain provider-neutral.
- Connectors, parsers, models, and repositories are injected through adapters.
- Canonical chunking has maintained Confluence and Jira policies plus a
  source-neutral fallback for attachments and community connectors.
- Provider writes and Temporal orchestration remain outside the engine.
- Agent logic is provider- and transport-neutral; MCP and runtime prompt
  adapters are injected at higher package boundaries.
- Conversation identity and memory contracts live in core; PostgreSQL
  persistence lives in adapters, outside chat and agent orchestration.

## Ingestion chunking

`harborrag_engine.ingestion.chunking` implements the deterministic chunking
stages used before the immutable chunk registry:

```text
canonical document
  -> canonical, Confluence, Jira, or registered source strategy
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

`ChunkingConfig` maps a normalized connector name to a profile; it never
routes on raw media types. `ChunkingService` executes the profile's registered
strategy, validates its output, assigns stable
`logical_chunk_id` and content-specific `chunk_revision_id` values, and returns
a `ChunkingResult` containing canonical core `ChunkRecord` values plus a
manifest of lightweight chunk references.

Construct the service with `build_chunking_service`, a model-compatible
`TokenCounter`, and a framework-neutral `TextRefiner`. `ChunkingService.chunk`
is synchronous, pure, and deterministic. The runtime persists the returned
validated chunk set and manifest through its immutable artifact repositories.

Confluence and Jira are maintained built-ins. Community connectors use the
canonical fallback or inject a `ChunkStrategy` through
`additional_strategies`; they do not modify the service. Raw Markdown, HTML,
JSON, PDF, and Office structure is resolved before chunking by parser and
normalizer adapters. A `TextRefiner` remains explicit because enforcing
`maximum_tokens` is a hard correctness requirement.

See `ingestion/chunking/README.md` for the package map and extension boundary.

## Representations and projections

`harborrag_engine.ingestion.representations` creates versioned dense and sparse
representations. `harborrag_engine.ingestion.projections` maps validated chunks
and canonical relations into deterministic staged vector and graph batches:

```text
validated chunks + representation manifest + canonical relations
  -> Qdrant route/evidence batches
  -> FalkorDB document/section/table/comment batches
  -> cross-projection verification
  -> Postgres-controlled publication in the runtime
```

Projection builders are pure and provider-independent. They never publish a
version, call Qdrant or FalkorDB, or treat a projection store as authoritative.
The runtime owns staged writes, verification, and the Postgres activation
transaction. Provider adapters own native payload and query translation.

## Package tests

Tests for this package live in:

```text
packages/harborrag-engine/tests/
└── ingestion/
    ├── unit/
    └── integration/
```

Run from the repository root:

```bash
pytest packages/harborrag-engine/tests/ingestion/unit
pytest packages/harborrag-engine/tests/ingestion/integration -m integration
```

The deployed end-to-end ingestion smoke lives in `harborrag-runtime`, which
owns provider composition and Temporal orchestration.
