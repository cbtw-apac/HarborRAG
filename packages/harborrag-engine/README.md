# harborrag-engine

The provider-neutral ingestion and retrieval engine of
[HarborRAG](https://github.com/cbtw-apac/HarborRAG): deterministic chunking, dense and
sparse representations, vector and graph projection batches, authoritative retrieval, the
reader tool catalog and the chat prompts. It depends only on `harborrag-core` and
`jsonschema`; providers, databases and Temporal are injected by the runtime.

To use HarborRAG as a library, install the [`harborrag`](https://pypi.org/project/harborrag/)
facade instead. Install this package directly when you build your own composition on
HarborRAG's engine policies.

## Install

```bash
pip install harborrag-engine
```

## What it provides

```text
harborrag_engine/
├── ingestion/
│   ├── admission.py         # source change and version planning
│   ├── normalizer.py        # canonical normalization between parser output and ingestion
│   ├── chunking/            # deterministic route and evidence chunking
│   ├── representations/     # dense and sparse representation policy
│   └── projections/         # vector and graph projection batches and their verification
├── retrieval/               # authoritative retrieval, fusion, evidence and the rerank port
├── topology/                # evidence validation and deterministic semantic projection policies
├── tools/                   # reader tool catalog, schemas, handlers and checked invocation
├── security/                # rules for MCP reader keys
└── chat/prompts/            # typed, packaged, provider-neutral chat prompts
```

These boundaries hold across the package:

- Core domain records and ports stay provider-neutral. Connectors, parsers, models and
  repositories come in through adapters.
- Provider writes and Temporal orchestration stay outside the engine.
- `RetrievalPipeline` has an optional `ResultReranker` stage but holds no rerank model.
  Production reranking is the runtime's `PassageReranker`, in `harborrag-runtime`.
- Chat prompt definitions are provider- and transport-neutral; the runtime binds the
  model clients.
- Reader tools bind only to core reader ports. The MCP server invokes them through one
  policy, validation and output-budget boundary.
- Conversation identity and memory contracts live in core; their PostgreSQL persistence
  lives in adapters, outside chat orchestration.

## Chunk a canonical document

`harborrag_engine.ingestion.chunking` turns one normalized document into canonical
route and evidence chunks before they reach the immutable chunk registry. Build the
service with `build_chunking_service`, passing a `ChunkingConfig`, a model-compatible
`TokenCounter` and a `TextRefiner`. `ChunkingService.chunk` is synchronous, pure and
deterministic. It returns a `ChunkingResult` holding canonical core `ChunkRecord` values,
each with a stable `logical_chunk_id` and a content-specific `chunk_id`, plus a manifest
of lightweight chunk references. The runtime persists that validated set through its
immutable artifact repositories.

`ChunkingConfig` maps a normalized connector name to a chunking profile; it never routes
on raw media types, because Markdown, HTML, JSON, PDF and Office structure is resolved
earlier by parser and normalizer adapters. Confluence and Jira have maintained source
strategies; every other connector uses the canonical fallback or adds its own
`ChunkStrategy` through `additional_strategies`. A `TextRefiner` is required because the
profile's `maximum_tokens` is a hard limit the result must meet.

The profile limits are described in
[Chunking configuration](https://github.com/cbtw-apac/HarborRAG/blob/main/docs/users/configuration/chunking-config.md).
The stages, the package map and how to add a source strategy are in the
[chunking package notes](https://github.com/cbtw-apac/HarborRAG/blob/main/packages/harborrag-engine/src/harborrag_engine/ingestion/chunking/README.md).

## Build representations and projections

`harborrag_engine.ingestion.representations` creates versioned dense and sparse
representations. `harborrag_engine.ingestion.projections` maps validated chunks and
canonical relations into deterministic staged vector and graph batches:

```text
validated chunks + representation manifest + canonical relations
  -> Qdrant route/evidence batches
  -> FalkorDB document/section/table/comment batches
  -> cross-projection verification
  -> Postgres-controlled publication in the runtime
```

Projection builders are pure and provider-independent. They never publish a version,
call Qdrant or FalkorDB, or treat a projection store as authoritative. The runtime owns
staged writes, verification and the Postgres activation transaction; provider adapters
own native payload and query translation.

## Contributing

See [CONTRIBUTING](https://github.com/cbtw-apac/HarborRAG/blob/main/CONTRIBUTING.md).
Tests for this package live in `packages/harborrag-engine/tests/`. Run them from the
repository root:

```bash
uv run pytest packages/harborrag-engine/tests
```

The default run skips tests marked `integration`. Select them explicitly:

```bash
uv run pytest packages/harborrag-engine/tests -m integration
```

`tests/ingestion/smoke/run.py` is a fast, provider-independent liveness check of the
engine ingestion path; see its
[README](https://github.com/cbtw-apac/HarborRAG/blob/main/packages/harborrag-engine/tests/ingestion/smoke/README.md).
The deployed end-to-end ingestion smoke lives in `harborrag-runtime`, which owns provider
composition and Temporal orchestration.

## License

Apache License 2.0.
