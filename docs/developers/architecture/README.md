# Architecture

HarborRAG uses a ports-and-adapters layout. Provider-neutral contracts flow downward; SDK integrations and operator surfaces stay at the edges.

## Active package map

| Package | Responsibility | Current maturity |
| --- | --- | --- |
| `harborrag-core` | Domain objects, validated model/storage schemas, common errors, security | Implemented contracts |
| `harborrag-adapters` | Connectors, parsers, model clients, repositories | Broadest implemented layer |
| `harborrag-engine` | Ingestion, retrieval, indexing, graph orchestration | Contracts, mock/static paths, focused utilities |
| `harborrag-runtime` | Config catalogs, composition, jobs, scheduling, supervision | Catalog loaders and local scaffolding |
| `harborrag-app` | Application service, CLI, HTTP boundary | Two mock-backed CLI commands; API placeholders |
| `harborrag-mcp` | Tool/server interfaces, policy, audit | In-process mock facade |
| `harborrag` | Public re-exports | `Document` and `CompositionRoot` |

`packages/harborrag-memory` reserves a future package boundary but has no public API or tests and is not an active uv workspace member.

## Dependency direction

`scripts/check_dependency_direction.py` enforces these imports:

```text
harborrag_core      -> no HarborRAG package
harborrag_adapters  -> core
harborrag_engine    -> core, adapters
harborrag_runtime   -> core, adapters, engine
harborrag_app       -> core, engine, runtime
harborrag_mcp       -> core, engine, runtime
harborrag           -> any active package
```

Run:

```bash
uv run make deps-check
```

The checker scans source imports, not runtime call graphs. Review still needs to catch indirect boundary leaks, provider objects in public schemas, and service layers bypassed through callbacks.

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

The local mock script does not implement every arrow: it loads and parses one document, creates a demonstration chunk dictionary, and retrieves from a deterministic list without persistence.

## Core contracts

`harborrag-core` has four major groups:

- `domain/` — document, source, parser, retrieval, provenance, element, chunk, data-source, and tenant values.
- `models/` — chat, embedding, reranking, capability, usage, request metadata, protocol, and safe error contracts.
- `schemas/` — typed IDs plus document, vector, graph, cache, state, object-store, telemetry, and storage-operation schemas.
- `security/` — secret redaction and URL policy.

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

`harborrag-engine` defines ingestion normalizer/chunker/pipeline interfaces, retrieval/evidence interfaces, indexing interfaces, graph mapping, reciprocal-rank fusion, rewriting, and reranking boundaries. The production, configured pipeline is not assembled yet.

`harborrag-runtime` owns:

- versioned connector and parser catalog loaders;
- `CompositionRoot.local()` and deterministic local diagnostics;
- in-memory/mock job, schedule, supervisor, and service contracts;
- placeholder Temporal modules that keep durable workflow SDK concerns out of lower packages.

Complete composition should be added here or in application bootstrap code, not in provider adapters.

## App and MCP boundaries

The app CLI calls `BaseAppService` rather than adapters. Today `doctor` and `sample-ingest` use `MockAppService`; HTTP route files and the FastAPI factory remain placeholders.

The MCP facade follows the same rule. `MockMcpServer` provides in-process dispatch for a health tool and deterministic retrieval tool. Policy and audit primitives exist but are not automatically enforced. No protocol transport is implemented.

## Configuration boundaries

- Runtime loads strict versioned connector/parser YAML catalogs.
- Model clients load strict family sections from YAML, JSON, or mappings.
- Engine config/policy are constructed in Python.
- There is no unified, auto-discovered application/workspace configuration.

See [Configuration](../../users/configuration/README.md).
