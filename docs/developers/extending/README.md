# Extending HarborRAG

This page shows where new behaviour goes and which contract it implements: a connector,
parser, model provider, storage backend, engine stage, runtime service or user-facing surface.
Choose the package that owns the behaviour, implement against its public contract, and test
the real implementation with deterministic fake dependencies. The package map and the terms
*port* and *adapter* are in [Architecture](../architecture/README.md); how to test is in
[Testing](../testing/README.md).

## Connectors

A connector discovers and loads items from one kind of source system. Add a provider under
`packages/harborrag-adapters/src/harborrag_adapters/connectors/<provider>/`:

```python
from collections.abc import Iterator

from harborrag_adapters.connectors.base import BaseConnector
from harborrag_adapters.connectors.schemas import ConnectorQuery
from harborrag_core.domain.raw_document import RawDocument
from harborrag_core.domain.source import SourceRecord


class ExampleConnector(BaseConnector):
    provider_name = "example"

    def discover(self, query: ConnectorQuery | None = None) -> Iterator[SourceRecord]:
        ...

    def load(self, record: SourceRecord) -> RawDocument:
        ...
```

A connector contribution includes typed configuration, client and session lifecycle,
filtering, pagination, retry and rate-limit behaviour, size and collection caps, permission
and provenance mapping, public exports, registry metadata, and tests for failures and security
boundaries. Items that discovery drops for a reportable reason, such as a size limit, are
reported through `skipped` rather than silently omitted.

Register one `ConnectorProviderDefinition` holding the connector class, typed config factory,
aliases, constructor dependency names, config path fields and default document kind. Runtime
configuration and construction derive their behaviour from this definition; do not add
parallel provider-name switches. Users then configure the provider as described in
[Connector Configuration](../../users/configuration/connector-config.md).

Use fakes in tests; do not ship a provider `mock.py` as a substitute for production behaviour.
Add an opt-in smoke script only when a check against the real system adds value.

## Parsers

A parser family turns one group of formats, such as PDF or spreadsheets, into a
`ParsedDocument`. Add a complete `HarborParser` family under `parsers/<family>/`. Add an
individual provider below that family's `engines/<provider>/` folder and implement the
family's engine contract, such as `HarborPDFEngine`.

Parsers:

- advertise normalized suffixes and MIME types;
- take `ParseInput` rather than guessing whether arbitrary text is a path;
- return a `ParsedDocument` with canonical content and useful elements and metadata;
- keep page, layout, table, code, image and warning information where available;
- raise parser-specific expected errors for malformed, unsupported or dependency-limited
  inputs;
- keep raw output bounded and disabled by default;
- declare optional dependencies and test without heavyweight models.

Register complete families through `HarborParserFactory`; provider engines never go into the
root MIME and extension registry, `HarborParserRegistry`. To make a provider configurable from
YAML, add it to the parser provider table in `harborrag_runtime/config/parsers/providers.py`.

## Model providers and behavior

Model code lives under `models/chat`, `models/embed` and `models/rerank` in
`harborrag-adapters`, with shared provider runtime capabilities under `models/runtime`. Core
request, response and error shapes live in `harborrag_core.models`.

When you add provider support:

- update the provider enum, validation and capability rules;
- translate only allowlisted parameters into the SDK or LiteLLM request;
- normalize provider exceptions into the core error taxonomy;
- keep the sync and async lifecycle consistent with the existing client;
- declare conservative capabilities and security allowlists;
- test retryability, fallback, cache keys, budgets, telemetry redaction and cancellation;
- add an example only with placeholder credentials and model IDs.

Do not emulate embeddings or reranking through chat. Keep embedding fallbacks compatible in
vector space and dimension.

## Repositories

A repository is the adapter behind a storage port. Add a backend below the matching family:

```text
repositories/vector/<provider>/
repositories/graph/<provider>/
repositories/cache/<provider>/
repositories/object_store/<provider>/
repositories/database/<provider>/
repositories/state/<provider>/
```

Implement the family's Harbor contract and its plugin and config pattern. A repository needs:

- an async lifecycle and async methods;
- `StorageOperationContext` on data operations;
- tenant isolation enforced in keys, collections, queries or rows;
- validated typed config and lazy imports of optional SDKs;
- normalized core records and sanitized health details;
- tests for conflicts, timeouts, missing records, partial failures, transactions and cleanup;
- tests against a fake SDK or client, plus an optional live smoke check.

Use `repositories/`, not a new `stores/` family. Do not return raw provider responses by
default.

### Selecting a storage provider in runtime

Runtime services consume the structural contracts in `harborrag_core.ports.storage`,
`harborrag_core.ports.artifacts` and `harborrag_core.ports.document_release`, so a replacement
does not need to inherit an adapter class. Its factory returns a fresh resource; runtime owns
connection and shutdown. Object stores implement `ensure_buckets`, which may be a no-op for
backends with implicit namespaces.

Register custom storage factories from your plugin's `register()` method:

```python
from harborrag_runtime.composition.storage_providers import storage_providers


class Plugin:
    capabilities = {"immutable_artifacts": True}

    def register(self):
        storage_providers.register_object_store("example", create_object_store)
```

Here `create_object_store(settings)` is a stable module-level factory in your plugin. Every
plugin declares `capabilities` and a `register()` method. Publish it as a Python
[entry point](https://packaging.python.org/en/latest/specifications/entry-points/) in one of
these groups: `harborrag.connectors`, `harborrag.parsers`, `harborrag.model_providers`,
`harborrag.vector_repositories`, `harborrag.graph_repositories` or
`harborrag.object_stores`.

Set `HARBORRAG_OBJECT_STORE_PROVIDER=example` and load the plugin before composition, either
explicitly with `discover_runtime_plugins()` or with the SDK's `discover_plugins=True`. Use
`register_vector_repository` for vectors; `register_graph(name, GraphProvider(knowledge=...,
topology=...))` selects both graph paths. Topology is optional and fails clearly when it is
requested from a provider that lacks it. A registration cannot reuse a built-in or an
already registered name.

S3, Qdrant and FalkorDB remain the defaults. For local object storage, select `filesystem` and
set `HARBORRAG_OBJECT_STORE_ROOT`; `memory` is useful in tests. A provider plugin must be
importable in every API and worker process that composes its storage resources.

## Engine stages

Provider-independent RAG behaviour goes in `harborrag-engine`:

- normalization in `ingestion/normalizer.py` and chunking in `ingestion/chunking/`;
- dense and sparse encoding in `ingestion/representations/`;
- vector and graph projection building and verification in `ingestion/projections/`;
- retrieval candidates, fusion, active-version checks, evidence and graph retrieval in
  `retrieval/`, with the `QueryRewriter` and `ResultReranker` ports in `retrieval/ports.py`;
- semantic topology policy in `topology/`;
- the shared reader tools served over MCP in `tools/`;
- stored chat prompts in `chat/prompts/`.

Inject connector, parser, model and repository contracts. Production stages preserve
provenance and permissions, pass tenant and request context through, expose bounded
concurrency, and make partial progress observable. Never import a concrete provider
subpackage into an engine stage.

## Runtime services

Configuration loading, provider composition, checkpoint coordination and durable workflow
code go in `harborrag-runtime`. Reuse the core job, repository, lifecycle and observation
ports instead of adding runtime-owned copies.

The catalog loaders in `harborrag_runtime.config` show the pattern: strict versioned schemas
and `${NAME}` environment references. New composition code keeps construction explicit and
imports an optional provider only when it is selected.

For source-specific canonical behaviour, implement `ConnectorDocumentTransform` inside the
provider's connector package and set `document_transform_factory` on its
`ConnectorProviderDefinition`. Runtime discovers registered transforms and stays
provider-neutral. Use `SourceDocumentNormalizerBuilder` only for application-local overrides
that do not belong to a connector package.

For source-specific chunking, provide a `ChunkingConfig` source profile and an additional
`ChunkStrategy` when building `IngestionRuntimeBuilder`. Keep provider validation in the
strategy's optional `record_validator` hook instead of branching in the shared chunk
validator.

Temporal SDK code belongs in `harborrag_runtime.temporal`. It must never become a dependency
of core, adapters or engine.

## Application and MCP surfaces

CLI and HTTP code call `BaseAppService`. The MCP server registers the shared reader tools from
`harborrag_engine.tools`, bound to runtime's reader composition. Neither surface builds a
provider client in a handler.

Stored chat prompts belong under `harborrag_engine/chat/prompts/templates/`. To add a public
prompt, add its UTF-8 Markdown template, add a stable name to `ChatPrompt` and its filename to
`PromptCatalog`, and test selection through every transport that exposes it. Keep model
selection, credentials, endpoints, tenant data and runtime interpolation out of templates.

A production interface also needs stable schemas, exit and error mapping, identity and tenant
context, permission enforcement, capability budgets, safe observability, lifecycle handling
and audit records. Update the user documentation in the same change that wires the command,
route, transport or tool.

## Public exports

Add an export to the `harborrag` meta-package only when it is stable, implemented, tested and
documented. Package-local public exports are equally intentional and are covered by import
smoke tests.

## Before a pull request

Run the full local set of checks in
[Quality gates](../../../CONTRIBUTING.md#quality-gates), then follow the rest of
[CONTRIBUTING.md](../../../CONTRIBUTING.md).
