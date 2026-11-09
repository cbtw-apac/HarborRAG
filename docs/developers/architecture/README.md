# Architecture

This page explains how HarborRAG's packages fit together and which rules hold across all of
them. Read it before you change code that crosses a package boundary. For the path one
document takes from source to answer, continue with [Data Lifecycle](data-lifecycle.md); for
retries, replay and failure behaviour, see [Runtime Reliability](runtime-reliability.md).
What each store owns, field by field, is in [Projection Rebuild](projection-rebuild.md).

## Terms used on these pages

- **Port**: a Python `Protocol` in `harborrag-core` that states what a service needs, such as
  a vector repository or a chat client, without naming a vendor.
- **Adapter**: a class in `harborrag-adapters` that implements a port against a real system
  or SDK. HarborRAG is a ports-and-adapters design: provider-neutral contracts sit in the
  middle, SDK integrations and user-facing surfaces sit at the edges.
- **Tenant**: the isolation boundary. Every stored row, vector collection, graph and
  object-store key belongs to exactly one tenant.
- **Canonical artifact**: an immutable file in the object store (raw content, parsed and
  normalized document, chunks, representations, manifests). Ingestion writes it once and
  never changes it.
- **Projection**: a copy of canonical data shaped for search: vectors in
  [Qdrant](https://qdrant.tech/documentation/) and a graph in
  [FalkorDB](https://docs.falkordb.com/). A projection can be rebuilt from canonical
  artifacts.
- **Active version**: the document version PostgreSQL marks as published. Retrieval returns
  evidence only from active versions.

## Architecture invariants

These rules matter more than any single provider choice:

- PostgreSQL is the authority for tenant, source, document, version, job and publication
  state.
- Immutable canonical artifacts in the object store are the evidence and replay boundary.
- Qdrant and FalkorDB hold version-addressed projections that can be rebuilt from canonical
  data.
- A version becomes active only after its required projections have been written and
  verified.
- Retrieval validates candidate versions against PostgreSQL before it returns evidence.
- Every data-plane operation carries tenant and principal context down to the storage
  boundary.
- Provider SDK types stop at adapters; core contracts and engine policy stay
  provider-neutral.
- Conversation memory accepts the neutral `MemoryModel` protocol. LangChain chat models work
  through a compatibility adapter in `harborrag_memory.langchain`; `langchain-core` supplies
  message conversion and trimming, and LangGraph is not required.
- Durable ingestion runs on [Temporal](https://docs.temporal.io/) behind a provider-neutral
  gateway. Retrieval is a direct, latency-sensitive path that never goes through Temporal.

## Package map

| Package | Responsibility | Role |
| --- | --- | --- |
| `harborrag-core` | Domain records, identifiers, errors, access context and ports | Provider-independent language |
| `harborrag-adapters` | Connectors, parsers, model clients and repository implementations | External I/O boundary |
| `harborrag-engine` | Ingestion, retrieval, topology and chat-prompt policy, plus the reader tools MCP serves | Business behaviour and invariants |
| `harborrag-memory` | Provider-neutral conversation state and memory policy | Required package; memory itself is optional configuration |
| `harborrag-runtime` | Composition, direct and Temporal executors, lifecycle, scheduling and configuration loading | Execution boundary |
| `harborrag-app` | HTTP API, CLI, authentication and presentation mapping | User transport |
| `harborrag-mcp-server` | MCP schemas, safety budgets, principal propagation, policy and audit | Agent transport |
| `harborrag` | Stable SDK re-exports and the installation bundle | `HarborRAG` public facade |

## Dependency direction

`scripts/check_dependency_direction.py` allows only these imports between packages:

```text
harborrag_core       -> no HarborRAG package
harborrag_adapters   -> core
harborrag_memory     -> core
harborrag_engine     -> core
harborrag_runtime    -> core, adapters, engine, memory
harborrag_app        -> core, runtime, engine
harborrag_mcp_server -> core, runtime, engine
harborrag            -> any HarborRAG package
```

The app and MCP server import `harborrag_engine` directly for the shared reader-tool layer and
its budgets; engine imports only core, so this inverts nothing. `.importlinter` holds the
[import-linter](https://import-linter.readthedocs.io/en/stable/) contracts that enforce the
same layering plus finer rules inside runtime. For example, the app may not import
`harborrag_runtime.temporal`, and the MCP server may not import `harborrag_runtime.chat`,
`harborrag_runtime.memory` or `harborrag_runtime.sdk`.

Run both checks:

```bash
uv run make import-boundaries
uv run make deps-check
```

A passing run marks every import-linter contract `KEPT`, ends that check with
`Contracts: 13 kept, 0 broken.` (the count grows as contracts are added), and prints
`Dependency direction OK: all cross-package imports follow the layering rules.` from the
dependency checker.

Import-linter is the required CI gate. The AST checker adds coverage for dynamic imports such
as `importlib.import_module("...")`; manual smoke scripts under `tests/**/smoke/` are exempt
from it. Neither check follows runtime call graphs, so review still has to catch indirect
boundary leaks, provider objects in public schemas, and service layers bypassed through
callbacks.

## Control plane and data plane

The control plane owns identities, configuration, source admission, version state,
publication, jobs and operator intent. Its durable authority is PostgreSQL. Temporal records
workflow progress and coordinates work, but it does not replace the authority record.

The data plane moves content and serves retrieval. It includes connectors, parsers, model
clients, the immutable object store, Qdrant and FalkorDB. Access context crosses from the
control plane into every storage operation; a provider response never becomes authority by
itself.

## Document flow

The core domain types follow one document through ingestion:

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

- `SourceRecord` is a lightweight locator found during discovery.
- A source descriptor holds the stable identity and metadata needed for admission, before any
  expensive content loading.
- `RawDocument` holds the loaded text or bytes and source metadata.
- `ParseInput` safely coerces paths, bytes, text or raw-document-like objects.
- `ParsedDocument` holds the extracted content, optional structured elements, warnings,
  metadata and bounded raw data.
- `Document` is the normalized domain object with provenance and structural relations.
- `ChunkRecord` is an addressable evidence range; `ChunkRepresentation` carries the text or
  structured form sent to an encoder.
- Projection records are rebuildable index inputs. Manifests prove which version and vector
  space were written before publication.

## Core contracts

`harborrag-core` groups its contracts by folder:

- `domain/`: document, source, parser, retrieval, provenance, element, job, member, project,
  provider, connection and settings values. `domain/__init__.py` still re-exports
  `ChunkRecord` from `chunking/` as a compatibility path.
- `chunking/`: immutable chunk, hierarchy, security, relation, source-attribute and table
  schemas. Deterministic chunk identity and chunk planning stay in engine.
- `models/`: chat, embedding, reranking, capability, usage, request metadata and safe error
  contracts.
- `ports/`: every boundary-facing `Protocol`, including repository and infrastructure ports
  (control plane, events, secrets, storage, indexing, topology) and model-client protocols
  such as `HarborChatClientProtocol` and its embedding, reranking and async counterparts.
- `indexing/` and `storage/`: public facades for vector and graph projection records,
  capabilities, `StorageOperationContext` and the per-tenant storage namespace. Modules under
  `schemas/` are internal organization, not cross-package import paths.
- `ingestion/`: artifact, projection, lifecycle and source-catalog contracts.
- `security/`: the required `AccessContext`, secret redaction, URL policy and
  `URLPolicyError`.
- `contracts/`: the shared `HarborError` hierarchy, `HarborEvent`, the chunking protocols
  `TextRefiner` and `TokenCounter`, transport-independent reader requests and results, and
  the tool contracts.
- `topology/`: semantic extraction, ontology and entity-resolution contracts.
- `config_store/`: the registry and level resolution behind runtime settings.

Most storage schemas derive from strict Pydantic bases; several older or simple domain values
are dataclasses. Add a shared concept to core only when more than one higher package needs a
provider-neutral form of it.

## Adapter layer

### Connectors

`BaseConnector` defines synchronous discovery (`discover`) and loading (`load`). Built-in
providers register in the connector registry when `harborrag_adapters.connectors` is
imported, and `HarborConnector` builds a provider by name.

Connectors own authentication, provider pagination, filtering, retry hints, safe URL and path
handling, source-specific caps, normalization to `SourceRecord` and `RawDocument`, and an
optional provider-specific transformation of canonical documents, which runtime discovers
through the registry. Connectors do not own workflow scheduling or global ingestion
concurrency.

### Parsers

`HarborParser` is the contract for a parser family, such as PDF, document, spreadsheet or
image. `HarborParserRegistry` maps MIME types and extensions to exactly one family, and
`HarborParserFactory` builds the registry from configuration. Each family owns engine
selection and fallback among its engines; engines derive from family contracts such as
`HarborPDFEngine` and never appear in the root registry. PDF parsing uses ordered backends or
named profiles. `BaseParser` remains only as a transitional alias of the internal engine base
class.

Parsers own extraction and warnings. Runtime and engine code own fan-out, backpressure,
persistence, and retries across documents.

### Models

Chat, embedding and reranking each have their own immutable configuration and public client.
Provider request translation stays in adapters; normalized requests, responses, errors,
capabilities and protocols stay in core.

The shared model runtime implements explicit retry and failover stages, routing, caching,
single-flight, budgets, health state, security allowlists and injected telemetry.
Configuration chooses a direct SDK call, a LiteLLM Router or a LiteLLM proxy where the
provider supports it; see [Model Configuration](../../users/configuration/model-config.md).

### Repositories

Repository families are asynchronous and tenant-aware. Data operations require a
`StorageOperationContext`.

| Family | Providers |
| --- | --- |
| Vector | Qdrant |
| Graph | FalkorDB |
| Cache | Memory, Redis |
| Object store | Memory, filesystem, S3 |
| Database | SQLite, PostgreSQL |
| Workflow state | SQLite, Redis |

Family clients build backends by provider name. Plugin and config modules isolate optional
imports. Public methods return core schemas and sanitized health data, not raw SDK responses.

These implementations are the production persistence path. Runtime factories build the
PostgreSQL or SQLite, object-store, Qdrant, FalkorDB, cache and state repositories and inject
them behind core ports. Engine never imports `harborrag_adapters.repositories`: that would
couple business policy to provider implementations and reverse the dependency direction.

The `object_store_provider`, `vector_provider` and `graph_provider` settings select the
storage factories; the defaults are S3, Qdrant and FalkorDB. Plugins register factories
through `harborrag_runtime.composition.storage_providers.storage_providers`, and a graph
provider may also supply topology projection. Core structural ports for storage, artifacts
and document release keep consuming services independent of adapter inheritance.

## Engine and runtime

`harborrag-engine` owns the policy for publication, projection verification, document
versions, cleanup, retrieval candidates, ranking, graph expansion, failure classification,
semantic topology, chat prompts, and the reader tools that the MCP server and the app expose.
It calls external models and storage only through core ports, and it never imports adapters
or a provider SDK.

`harborrag-runtime` owns:

- loaders for the `config/` catalogs and the environment-driven `RuntimeSettings`;
- production control-plane composition and health diagnostics;
- Temporal contracts, workflows, activities, clients and worker lifecycle;
- the dependency graph that connects adapters to engine services;
- `DirectIngestionExecutor` for local and library execution, and `TemporalIngestionExecutor`
  for durable distributed execution;
- the async `HarborRAG` facade used by the SDK and the app;
- `harborrag_runtime.chat`, which composes the asynchronous chat client lazily, owns its
  lifecycle, and resolves the typed prompts that engine packages;
- explicit, opt-in discovery of Python entry-point plugins.

Runtime schedules publication and cleanup and chooses retry, timeout and concurrency
behaviour. It does not decide whether a version is publishable or a projection is valid;
those rules stay in engine so that direct execution, Temporal, tests and reindex all behave
the same way.

Framework-independent lifecycle and observer interfaces and the job and repository ports live
in `harborrag-core`. Provider-specific assembly belongs in runtime or in application bootstrap
code, not in provider adapters.

## App and MCP boundaries

The CLI and the HTTP API both call one application service, `BaseAppService`, never adapters.
Ingestion commands and HTTP ingestion routes resolve a secret-free source contract, persist a
pending task in PostgreSQL, and hand it to runtime's neutral `IngestionGateway`. The default
`TemporalIngestionGateway` translates submission, retry, status and result values into
Temporal workflow contracts. Application composition can replace the gateway, its health
description and its source-preparation defaults without importing Temporal internals; an
import-linter contract protects this boundary.

The deployed worker registers these workflows: source ingestion, scheduled source ingestion,
retry failures, source batch, document ingestion, document retry and reindex.

Chat exists only in the app; MCP does not expose it. HTTP and CLI share one runtime path:

```text
HTTP route  ──┐
              ├─> BaseAppService ─> ChatApplicationService ─> HarborRAG.chat
CLI command ──┘                                                  ├─> RuntimeChatService
                                                                 ├─> PromptCatalog
                                                                 └─> async chat client
```

`ChatApplicationService` adds sessions, memory, citations and usage records around the
`HarborRAG.chat` facade. Prompts come from engine's `PromptCatalog`, chat-client lifecycle
stays in runtime, provider translation stays in adapters, and public schemas stay
provider-neutral. No handler builds a model client.

The MCP server builds its tool catalog from the shared reader tools in
`harborrag_engine.tools` and runs them against runtime's reader composition,
`harborrag_runtime.composition.readers`. Every reader request carries an `AccessContext` built
from the authenticated MCP principal and tenant. The server never calls app routes,
repositories or provider clients directly.

## Authority and projections

PostgreSQL is authoritative for document and version state and for atomic publication.
Immutable canonical artifacts are kept so projections can be rebuilt without the connector.
Qdrant and FalkorDB hold version-addressed, verified projections; a failed cleanup is
retried, not treated as a distributed rollback.

Vector, graph and object-store operations require a `StorageOperationContext`. Core defines
the principal and tenant requirement, engine decides the filtering policy, and data-plane
adapters enforce the tenant at the storage boundary. Temporal identifiers and worker retry
metadata are deliberately kept out of the storage context.

## Configuration boundaries

Configuration comes from three places: per-domain YAML files under `config/`, environment
variables read into `RuntimeSettings` at boot, and runtime settings stored per tenant in the
control database. Secrets stay in environment files; the YAML files only reference them as
`${NAME}`. Chat prompts ship inside `harborrag-engine` and are never configuration, and
engine policy objects are built in Python. Each file is described in
[Configuration](../../users/configuration/README.md), and the settings an administrator can
change without a restart in [Runtime Settings](../../users/configuration/runtime-settings.md).
