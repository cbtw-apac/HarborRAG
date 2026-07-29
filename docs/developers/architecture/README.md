# Architecture

HarborRAG uses a ports-and-adapters layout. Provider-neutral contracts flow downward; SDK integrations and operator surfaces stay at the edges.

Accepted choices and their consequences are recorded in the
[architecture decision records](../../adr/README.md).

## Active package map

| Package | Responsibility | Current maturity |
| --- | --- | --- |
| `harborrag-core` | Domain objects, validated model/storage schemas, common errors, security | Implemented contracts |
| `harborrag-adapters` | Connectors, parsers, model clients, repositories | Broadest implemented layer |
| `harborrag-engine` | Ingestion, retrieval, indexing, graph orchestration | Implemented engine stages and policies |
| `harborrag-runtime` | Config catalogs, production composition, Temporal orchestration | Durable workflows and application integration boundary |
| `harborrag-app` | Application service, CLI, HTTP boundary | Temporal-backed CLI plus health and diagnostics API |
| `harborrag-mcp-server` | Tool/server interfaces, policy, audit | Audited in-process health transport |
| `harborrag` | Public re-exports | `Document` and `CompositionRoot` |

## Dependency direction

`scripts/check_dependency_direction.py` enforces these imports:

```text
harborrag_core      -> no HarborRAG package
harborrag_adapters  -> core
harborrag_engine    -> core, adapters
harborrag_runtime   -> core, adapters, engine
harborrag_app       -> core, engine, runtime
harborrag_mcp_server -> core, engine, runtime
harborrag           -> any active package
```

Run:

```bash
uv run make import-boundaries
uv run make deps-check
```

Import-linter is the required CI boundary gate. The AST dependency checker provides
additional coverage for dynamic imports and package-local tests. Neither scans
runtime call graphs, so review still needs to catch indirect boundary leaks, provider
objects in public schemas, and service layers bypassed through callbacks.

## Document flow

The core domain types describe the intended ingestion lifecycle:

```text
SourceRecord
   │ connector.load
   ▼
RawDocument
   │ parser.parse
   ▼
ParsedDocument
   │ normalizer
   ▼
Document + DocumentElement + DocumentProvenance
   │ chunk/embed/index
   ▼
storage schemas and RetrievalResult
```

- `SourceRecord` is a lightweight discovered locator.
- `RawDocument` holds loaded text/bytes and source metadata.
- `ParseInput` safely coerces paths, bytes, text, or raw-document-like objects.
- `ParsedDocument` holds canonical extracted content, optional structured elements, warnings, metadata, and bounded raw data.
- `Document` is the normalized domain object with provenance and structural relations.

## Core contracts

`harborrag-core` has these groups:

- `domain/` — document, source, parser, retrieval, provenance, element, job, member, project, provider,
  and source-config values. `domain/__init__.py` retains the former chunk imports as compatibility
  paths to the canonical Pydantic contracts in `chunking/`.
- `chunking/` — canonical immutable chunk, hierarchy, security, relation, source-attribute, and table
  schemas. Deterministic identity policy and chunk planning remain engine responsibilities.
- `models/` — chat, embedding, reranking, capability, usage, request metadata, and safe error contracts.
  Client-boundary protocols now live in `ports/` (ADR-0009), not here.
- `schemas/` — typed IDs plus document, vector, graph, cache, state, object-store, telemetry, and storage-operation schemas.
- `security/` — secret redaction, URL policy, and `URLPolicyError`.
- `contracts/` — the shared `HarborError` hierarchy, `HarborEvent`, and chunking-strategy protocols
  (`TextRefiner`, `StructureSplitter`, `JsonStructureSplitter`, `TokenCounter`).
- `ports/` — every boundary-facing `Protocol` in core: repository/infra ports (control plane, event bus,
  job queue, secrets, runtime lifecycle, vector/graph indexing) and, since ADR-0009, model-client protocols
  (`HarborChatClientProtocol` and its embed/rerank/async counterparts).

Most storage schemas derive from strict Pydantic bases; several older/simple domain values are dataclasses. Add a shared concept to core only when more than one higher package needs a provider-neutral form.

## Adapter layer

### Connectors

`BaseConnector` defines synchronous discovery and loading. Built-in providers register aliases at `harborrag_adapters.connectors` import time. `HarborConnector` constructs a provider by name.

Connectors own authentication, provider pagination, filtering, retry hints, safe URL/path handling, source-specific caps, and normalization to `SourceRecord`/`RawDocument`. They do not own workflow scheduling or global ingestion concurrency.

### Parsers

`BaseParser` defines metadata-only routing and parsing. `HarborParser` combines the default parser stack and rejects ambiguous non-generic routes. PDF parsing uses ordered backends or named profiles.

Parsers own extraction and warnings. Runtime/engine code owns fan-out, backpressure, persistence, and retry across documents.

### Models

Chat, embedding, and reranking have separate immutable configuration and public clients. Provider request translation stays in adapters; normalized requests, responses, errors, capabilities, and protocols stay in core.

The shared runtime implements explicit retry/failover stages, routing, caching, singleflight, budgets, health state, security allowlists, and injected telemetry. Configuration may choose direct SDK, LiteLLM Router, or LiteLLM proxy behavior where supported.

### Repositories

Repository families are asynchronous and tenant-aware. `StorageOperationContext` is required on data operations.

| Family | Providers |
| --- | --- |
| Vector | Qdrant |
| Graph | FalkorDB |
| Cache | Memory, Redis |
| Object store | Memory, filesystem, S3 |
| Database | SQLite, PostgreSQL |
| Workflow state | SQLite, Redis |

Family clients construct backends by provider name. Plugin/config modules isolate optional imports. Public methods return core schemas and sanitized health data rather than raw SDK responses.

## Engine and runtime

`harborrag-engine` defines normalization, chunking, indexing, retrieval/evidence,
graph mapping, reciprocal-rank fusion, rewriting, and reranking boundaries. The
runtime composes its ingestion services without moving provider logic into the
engine.

`harborrag-runtime` owns:

- versioned connector and parser catalog loaders;
- production control-plane composition and health diagnostics;
- versioned Temporal contracts, workflows, activities, clients, and worker lifecycle;
- the runtime dependency graph that connects adapters and engine services.

Framework-independent lifecycle/observer interfaces and job/repository ports live
in `harborrag-core`. Provider-specific assembly belongs here or in application
bootstrap code, not in provider adapters.

## App and MCP boundaries

The app CLI calls `BaseAppService` rather than adapters. Production ingestion
commands delegate to `TemporalRuntimeClient`. HTTP ingestion routes remain
future work.

The MCP facade follows the same rule. `McpServer` provides audited,
policy-checked in-process dispatch for the health tool. No external protocol
transport is implemented yet.

## Configuration boundaries

- Runtime loads strict versioned connector/parser YAML catalogs.
- Model clients load strict family sections from YAML, JSON, or mappings.
- Engine config/policy are constructed in Python.
- There is no unified, auto-discovered application/workspace configuration.

See [Configuration](../../users/configuration/README.md).
