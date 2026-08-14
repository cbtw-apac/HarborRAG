# Architecture

HarborRAG uses a ports-and-adapters layout. Provider-neutral contracts flow downward; SDK integrations and operator surfaces stay at the edges.

The store-by-store ownership contract — what belongs in PostgreSQL, the object store,
Qdrant, and FalkorDB — and operational guidance for the vector/graph boundary are in the
[projection rebuild runbook](projection-rebuild.md).

For the end-to-end behavior, continue with the [data lifecycle](data-lifecycle.md). For
durability, replay, and failure boundaries, see [runtime reliability](runtime-reliability.md).

## Architecture invariants

These rules are more important than any individual provider choice:

- PostgreSQL is the authority for tenant, source, document, version, job, and publication state.
- Immutable canonical artifacts in the object store are the evidence and replay boundary.
- Qdrant and FalkorDB are version-addressed projections that can be rebuilt from canonical data.
- A version becomes active only after its required projections have been written and verified.
- Retrieval validates candidate versions against PostgreSQL before returning evidence.
- Every data-plane operation carries tenant and principal context to the storage boundary.
- Provider SDK types stop at adapters; core contracts and engine policy remain provider-neutral.
- Temporal coordinates durable ingestion. Retrieval remains a direct, latency-sensitive path.

## Active package map

| Package | Responsibility | Current maturity |
| --- | --- | --- |
| `harborrag-core` | Bounded domain records, identifiers, errors, access context, and ports | Provider-independent language |
| `harborrag-adapters` | Connectors, parsers, model clients, and repository implementations | External I/O boundary |
| `harborrag-engine` | Provider-independent ingestion/retrieval policies and transformations | Business behavior and invariants |
| `harborrag-memory` | Provider-neutral conversation state and memory policy | Optional application memory |
| `harborrag-runtime` | Composition, direct/Temporal executors, lifecycle, scheduling, and config loading | Execution boundary |
| `harborrag-app` | HTTP, CLI, authentication, and presentation mapping | User transport |
| `harborrag-mcp-server` | MCP schemas, safety budgets, principal propagation, policy, and audit | Agent transport |
| `harborrag` | Stable SDK re-exports and optional installation bundle | `HarborRAG` public facade |

## Dependency direction

`scripts/check_dependency_direction.py` enforces these imports:

```text
harborrag_core      -> no HarborRAG package
harborrag_adapters  -> core
harborrag_memory    -> core
harborrag_engine    -> core, memory
harborrag_runtime   -> core, adapters, engine, memory
harborrag_app       -> core, runtime
harborrag_mcp_server -> core, runtime
harborrag           -> any active package
```

Run:

```bash
uv run make import-boundaries
uv run make deps-check
```

Import-linter is the required CI boundary gate. The AST dependency checker provides
additional source-tree coverage for dynamic imports. Cross-layer integration tests
may compose several packages and are not treated as production dependencies. Neither scans
runtime call graphs, so review still needs to catch indirect boundary leaks, provider
objects in public schemas, and service layers bypassed through callbacks.

## Control plane and data plane

The control plane owns identities, configuration, source admission, version state,
publication, jobs, and operator intent. Its durable authority is PostgreSQL. Temporal
records workflow progress and coordinates work, but it does not replace the authority
record.

The data plane moves content and serves retrieval. It includes connectors, parsers, model
clients, the immutable object store, Qdrant, and FalkorDB. Access context crosses from the
control plane into every storage operation; provider responses never become authority by
themselves.

## Document flow

The core domain types describe the intended ingestion lifecycle:

```text
SourceRecord
   │ connector.describe + admission
   ▼
source descriptor
   │ connector.load
   ▼
RawDocument
   │ parser.parse
   ▼
ParsedDocument
   │ normalizer
   ▼
Document + DocumentElement + DocumentProvenance
   │ source-aware chunk planning
   ▼
ChunkRecord + ChunkRepresentation
   │ dense/sparse encoding + projection
   ▼
vector/graph projection records + manifests
   │ verify + publish
   ▼
active version + RetrievalResult
```

- `SourceRecord` is a lightweight discovered locator.
- A source descriptor contains the stable identity and metadata needed for admission before
  expensive content loading begins.
- `RawDocument` holds loaded text/bytes and source metadata.
- `ParseInput` safely coerces paths, bytes, text, or raw-document-like objects.
- `ParsedDocument` holds canonical extracted content, optional structured elements, warnings, metadata, and bounded raw data.
- `Document` is the normalized domain object with provenance and structural relations.
- `ChunkRecord` preserves an addressable evidence range; `ChunkRepresentation` carries the
  text or structured representation sent to an encoder.
- Projection records are rebuildable index inputs. Manifests prove which version and vector
  space were written before publication.

## Core contracts

`harborrag-core` has these groups:

- `domain/` — document, source, parser, retrieval, provenance, element, job, member, project, provider,
  and source-config values. `domain/__init__.py` retains the former chunk imports as compatibility
  paths to the canonical Pydantic contracts in `chunking/`.
- `chunking/` — canonical immutable chunk, hierarchy, security, relation, source-attribute, and table
  schemas. Deterministic identity policy and chunk planning remain engine responsibilities.
- `models/` — chat, embedding, reranking, capability, usage, request metadata, and safe error contracts.
  Client-boundary protocols now live in `ports/`, not here.
- `indexing/` and `storage/` — public bounded-context facades for vector/graph projection records,
  capabilities, storage access context, and health. Legacy implementation modules under `schemas/`
  are internal organization, not cross-package import paths.
- `security/` — required `AccessContext`, secret redaction, URL policy, and `URLPolicyError`.
- `contracts/` — the shared `HarborError` hierarchy, `HarborEvent`, and chunking-strategy protocols
  (`TextRefiner`, `StructureSplitter`, `JsonStructureSplitter`, `TokenCounter`).
- `ports/` — every boundary-facing `Protocol` in core: repository/infra ports (control plane, event bus,
  job queue, secrets, runtime lifecycle, vector/graph indexing) and model-client protocols
  (`HarborChatClientProtocol` and its embed/rerank/async counterparts).

Most storage schemas derive from strict Pydantic bases; several older/simple domain values are dataclasses. Add a shared concept to core only when more than one higher package needs a provider-neutral form.

## Adapter layer

### Connectors

`BaseConnector` defines synchronous discovery and loading. Built-in providers register aliases at `harborrag_adapters.connectors` import time. `HarborConnector` constructs a provider by name.

Connectors own authentication, provider pagination, filtering, retry hints,
safe URL/path handling, source-specific caps, normalization to
`SourceRecord`/`RawDocument`, and optional provider-specific transformation of
generic canonical documents. Runtime discovers that transformation through the
connector registry. Connectors do not own workflow scheduling or global
ingestion concurrency.

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

These repository implementations are the production persistence path. Runtime
factories construct the PostgreSQL/SQLite, object-store, Qdrant, FalkorDB, cache,
and state repositories and inject them behind core ports. Engine deliberately does
not import `harborrag_adapters.repositories`: doing so would couple business policy
to provider implementations and reverse the dependency direction.

## Engine and runtime

`harborrag-engine` owns publication, projection-verification, document-version,
cleanup, retrieval-candidate, ranking, graph-expansion, and failure-classification
policy. External model and storage operations are called through core ports. It
never imports adapters or a provider SDK.

`harborrag-runtime` owns:

- schema-validated connector and parser catalog loaders;
- production control-plane composition and health diagnostics;
- Temporal contracts, workflows, activities, clients, and worker lifecycle;
- the runtime dependency graph that connects adapters and engine services.
- `DirectIngestionExecutor` for local/library execution and
  `TemporalIngestionExecutor` for durable distributed execution;
- the stable async `HarborRAG` facade used by transports;
- `harborrag_runtime.chat`, which lazily composes the asynchronous chat client,
  owns its lifecycle, and resolves typed server-owned prompts;
- explicit, opt-in Python entry-point plugin discovery.

Runtime schedules publication and cleanup and chooses retry/time-out/concurrency
behavior. It does not independently decide whether a version is publishable or a
projection is valid; those rules remain in engine so direct execution, Temporal,
tests, and reindex use the same behavior.

Framework-independent lifecycle/observer interfaces and job/repository ports live
in `harborrag-core`. Provider-specific assembly belongs here or in application
bootstrap code, not in provider adapters.

## App and MCP boundaries

The app CLI calls `BaseAppService` rather than adapters. Production ingestion
commands and HTTP ingestion routes resolve a secret-free source contract,
persist a pending task in Postgres, and delegate to
`IngestionTemporalClient`. The deployed worker registers only the canonical
source, batch, document, failed-document retry, and reindex workflows.

Chat and agent are app-only surfaces; MCP does not expose them. Both follow
one shared runtime path:

```text
HTTP route ──> ChatApplicationService ──┐
CLI command ─> BaseAppService ──────────┼─> HarborRAG.chat
                                        │       │
                                        │       ├─> RuntimeChatService
                                        │       ├─> PromptCatalog
                                        │       └─> AsyncHarborChatClient
```

HTTP and CLI map through the application service, which calls the
`HarborRAG.chat` facade. Stored prompts and chat-client lifecycle stay
in runtime, provider translation stays in adapters, and public schemas remain
provider-neutral. None of these handlers construct a model client.

The MCP facade follows the same rule for retrieval. Vector retrieval calls
`HarborRAG.retrieval.search()` and supplies a required `AccessContext` built
from the authenticated MCP principal and tenant. It never calls app routes,
repositories, or provider clients directly.

## Authority and projections

PostgreSQL is authoritative for document/version state and atomic publication.
Immutable canonical artifacts are retained for connector-free reindex. Qdrant and
FalkorDB are version-addressed, verified projections: failed cleanup is retried and
is not treated as a distributed rollback.

Vector, graph, and object-store operations require `StorageOperationContext`. Core
defines the principal/tenant access requirement, engine decides filtering policy,
and data-plane adapters enforce the tenant at the storage boundary. Temporal
identifiers and worker retry metadata are deliberately excluded from canonical
storage context.

## Configuration boundaries

- Runtime loads strict versioned connector/parser YAML catalogs.
- Model clients load strict family sections from YAML, JSON, or mappings.
- Runtime chat prompts are packaged Markdown resources selected by a typed
  public name; credentials and provider routing never belong in prompt files.
- MCP tool defaults, limits, enablement, and tenant overrides live in the
  separate versioned `config/mcp.yaml` catalog.
- Engine config/policy are constructed in Python.
- There is no unified, auto-discovered application/workspace configuration.

See [Configuration](../../users/configuration/README.md).
