# Architecture Overview

HarborRAG uses a ports-and-adapters (hexagonal) layout: each responsibility — contracts, providers, orchestration, runtime, API/CLI, agent tools — lives in its own package, and packages may only depend downward in a fixed direction.

## Package map

```text
packages/
  harborrag-core/      contracts, domain models, ports, execution, observability, security
  harborrag-adapters/  connectors, parsers, models, repository providers
  harborrag-engine/    ingestion, retrieval, indexing, graph orchestration
  harborrag-runtime/   jobs, supervision, scheduling, runtime services
  harborrag-app/       application service, API controller, CLI command boundary
  harborrag-mcp/       MCP tools/server facade with policy and audit boundaries
  harborrag/           public facade / meta-package
```

## Dependency direction

```text
harborrag_core      -> (no HarborRAG dependencies)
harborrag_adapters  -> core
harborrag_engine    -> core, adapters
harborrag_runtime   -> core, adapters, engine
harborrag_app       -> core, engine, runtime
harborrag_mcp       -> core, engine, runtime
harborrag           -> any package (public facade)
```

This is enforced mechanically, not just by convention:

```bash
python scripts/check_dependency_direction.py
make deps-check
```

Any import that violates the table above fails CI (`.github/workflows/quality-gates.yml`).

## Provider contracts and test doubles

Provider-facing families keep their public contract separate from concrete SDK integrations:

```text
<family>/
  base.py           provider-neutral abstract contract
  <provider>/       production implementation and validated configuration
  tests/            deterministic fakes and provider request/response tests
```

Some orchestration surfaces still ship deterministic `Mock*` implementations for the local mock pipeline. Implemented repository families instead use named providers such as `qdrant/`, `falkordb/`, `redis/`, `s3/`, `sqlite/`, and `postgresql/`; fake SDK clients remain test-only. See [Extending HarborRAG](../extending/README.md).

## `harborrag-core`: domain and shared contracts

Core has zero dependencies on other HarborRAG packages and no provider SDK imports. Shared validation and error primitives live in `base.py` and `errors.py`; the rest is organized into four areas:

- `base.py` — the shared strict and extensible Pydantic model bases.
- `errors.py` — dependency-light errors used across packages.

### `domain/` — the shapes that flow through ingestion and retrieval

`SourceRecord` → `RawDocument` → `ParsedDocument` → `Document` is the ingestion pipeline's document lifecycle:

- `source.py` (`SourceRecord`) — what a connector discovers before loading.
- `raw_document.py` (`RawDocument`) — the connector's loaded output.
- `parser.py` (`ParseInput`, `ParsedDocument`, `ParserFormat`) — parser input, output, and routing formats.
- `element.py` (`DocumentElement`) — a heading/paragraph/table/image/code/metadata element within a document.
- `document.py` (`Document`) — the normalized document the engine embeds and indexes.
- `provenance.py` (`DocumentProvenance`) — open-ended source identity, permissions, timestamps, and extra source metadata.
- `retrieval.py` (`RetrievalQuery`, `RetrievalResult`) — the retrieval-side query/result pair.

### `models/` — provider-neutral model contracts

- `chat/`, `embed.py`, and `rerank.py` — validated request/response models.
- `capabilities.py` — model-family capability declarations.
- `protocols.py` — synchronous and asynchronous structural client contracts.
- `errors.py` — provider-neutral model error taxonomy and safe diagnostics.

### `schemas/` — storage and repository contracts

- `ids.py` and `storage.py` — typed storage identifiers, operation context, and health records.
- `documents.py`, `vector.py`, and `graph.py` — persistent document and retrieval records.
- `cache.py`, `state.py`, and `object_store.py` — cache, workflow-state, and object-store records.
- `telemetry.py` — sanitized repository operation events.

### `security/`

- `security/redaction.py` — `redact_secrets(text)` masks API keys, tokens, secrets, passwords, and bearer tokens in log/error text.
- `security/url_policy.py` — `URLPolicy` validates a URL's scheme against an allow-list and its host against a deny-list before a connector fetches it.

## `harborrag-engine`: orchestration

- `builder.py` (`EngineBuilder`) + `config.py` (`EngineConfig`: `tenant`, `environment`) + `policy.py` (`EnginePolicy`: `max_concurrency`, `retrieval_top_k`) — engine-level configuration and diagnostics.
- `ingestion/` — `BaseDocumentNormalizer`, `BaseChunker`, `BaseIngestionPipeline` (contracts) and `MockDocumentNormalizer`, `MockChunker`, `MockIngestionPipeline` (mocks). `IngestionRunSummary` reports `discovered`/`loaded`/`parsed`/`indexed` counts.
- `retrieval/` — `BaseRetrievalPipeline`, `BaseEvidenceBuilder` (contracts) and `MockRetrievalPipeline`, `MockEvidenceBuilder` (mocks), plus `fusion.py` (`reciprocal_rank_fusion`), `reranking.py`, and `rewriting.py` for later hybrid-retrieval stages.
- `indexing/` — `BaseIndexer`/`MockIndexer` define the current indexing boundary. Graph persistence uses the provider-neutral node and edge schemas directly; no unused graph-mapper layer sits between them.

Engine code depends only on `harborrag-core` domain/model/schema contracts and `harborrag-adapters` base classes — never on a concrete provider.

## `harborrag-runtime`: composition, jobs, scheduling

- `composition.py` (`CompositionRoot`) — the single place that wires connector + parser + embedder + vector repository into a pipeline today (`mock_pipeline()`); this hard-coded assembly is meant to become configuration-driven.
- `job_state.py` (`JobState`, `InMemoryJobStore`) and `jobs/` (`BaseJobStore`/`MockJobStore`) — job persistence contracts.
- `scheduling/` (`BaseScheduler`/`MockScheduler`) and `schedules.py` (`ScheduleSpec`) — scheduled-job contracts.
- `supervision/` (`BaseSupervisor`/`MockSupervisor`) and `supervisor.py` (`LocalSupervisor`) — bounded local worker execution.
- `services/` (`BaseRuntimeService`/`MockRuntimeService`) — the facade `harborrag-app` and `harborrag-mcp` call instead of touching adapters directly.
- `temporal/` — optional durable-workflow integration; every file is currently a TODO placeholder kept behind this package so Temporal never becomes a hard dependency of core/adapters/engine.

## `harborrag-app`: CLI and API boundary

- `services/` (`BaseAppService`/`MockAppService`) — the only thing the CLI and HTTP layers call.
- `cli/main.py` — the real `harbor` entry point today (`doctor`, `sample-ingest`); `cli/commands/*.py` are TODO stubs for a future `doctor`/`ingest`/`retrieve`/`status` subcommand split.
- `api/` — `dependencies.py` (`get_app_service()`), `app.py` (`create_app_state()` placeholder for a future FastAPI app), and `routes/*.py` TODO stubs for HTTP route handlers that will call the service layer, not adapters.

See [CLI Reference](../../users/cli-reference/README.md) for what's runnable today.

## `harborrag-mcp`: audited agent tools

- `tools/` (`BaseMcpTool`/`MockHealthTool`, `MockRetrieveTool`) — each tool declares an `McpToolSpec` (name, description, JSON input schema) and implements `call()`.
- `server/` (`BaseMcpServer`/`MockMcpServer`) — dispatches `call_tool(name, arguments)` to the matching tool.
- `policy.py` (`McpToolPolicy`) — result-count budgets (`max_results`) and ingestion allow/deny.
- `audit.py` (`McpAuditLog`) — records which tools were called.
- `schemas.py` (`tool_schema`) — the MCP tool-schema envelope shape.

See [MCP Mock Tools](../../users/detailed-guides/mcp-server/README.md) for the tool list.

## `harborrag`: public facade

A thin meta-package that re-exports the stable public surface — currently `CompositionRoot` and `Document` — so downstream code can `import harborrag` instead of reaching into individual packages. New re-exports should only be added once an API is implemented and documented, per the package's own README.

## Related

- [Extending HarborRAG](../extending/README.md) — how to add a real provider without breaking these rules.
- [Testing](../testing/README.md) — how contracts and providers are verified.
- [Deployment](../deployment/README.md) — the `deploy/` stack this architecture is designed to run against.
