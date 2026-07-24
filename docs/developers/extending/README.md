# Extending HarborRAG

Choose the package that owns the behavior, implement against its public contract, and test the real implementation with deterministic fake dependencies.

## Connectors

Add providers under `packages/harborrag-adapters/src/harborrag_adapters/connectors/<provider>/`.

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

A connector contribution should include typed config, client/session lifecycle, filtering, pagination, retry/rate-limit behavior, size/collection caps, permission/provenance mapping, public exports, registry aliases, and tests for failures and security boundaries.

Use fakes in tests; do not ship a provider `mock.py` as a substitute for production behavior. Add an opt-in smoke script only when a real-system check adds value.

## Parsers

Implement `BaseParser[Input, Output]` under `parsers/`, or a `PdfBackend` under `parsers/pdf_engine/`.

Parsers should:

- advertise normalized suffixes and MIME types;
- use `ParseInput` rather than guessing whether arbitrary text is a path;
- return `ParsedDocument` with canonical content and useful elements/metadata;
- preserve page, layout, table, code, image, and warning information where available;
- raise parser-specific expected errors for malformed, unsupported, or dependency-limited inputs;
- keep raw output bounded and disabled by default;
- declare optional dependencies and test without requiring heavyweight models.

Register ordinary parsers in `HarborParser.default_parsers()` when they should be part of the default route. Add PDF backend metadata to the runtime parser provider table when it should be YAML-configurable.

## Model providers and behavior

Model code lives under `models/chat`, `models/embed`, `models/rerank`, and
provider runtime capabilities under `models/runtime`. Core request, response,
and error shapes live in the corresponding `harborrag_core` schema modules.

When adding provider support:

- update the appropriate provider enum/validation and capability rules;
- translate only allowlisted parameters into the SDK/LiteLLM request;
- normalize provider exceptions into the core error taxonomy;
- support sync/async lifecycle consistently with the existing client;
- declare conservative capabilities and security allowlists;
- test retryability, fallback, cache keys, budgets, telemetry redaction, and cancellation;
- add an example only with placeholder credentials and model IDs.

Do not emulate embeddings or reranking through chat. Keep embedding fallbacks compatible in space and dimension.

## Repositories

Add a backend below the matching family:

```text
repositories/vector/<provider>/
repositories/graph/<provider>/
repositories/cache/<provider>/
repositories/object_store/<provider>/
repositories/database/<provider>/
repositories/state/<provider>/
```

Implement the family's Harbor contract and plugin/config pattern. Repository requirements include:

- async lifecycle and methods;
- `StorageOperationContext` on data operations;
- tenant isolation enforced in keys, collections, queries, or rows;
- validated typed config and lazy optional SDK imports;
- normalized core records and sanitized health details;
- conflict, timeout, missing-record, partial-failure, transaction, and cleanup tests;
- fake SDK/client tests plus an optional live smoke check.

Use `repositories/`, not a new `stores/` family. Do not return raw provider responses by default.

## Engine stages

Put provider-independent RAG orchestration in `harborrag-engine`:

- normalizers and chunkers under `ingestion/`;
- vector/index writing under `ingestion/indexing/`;
- query rewrite, retrieval, fusion, reranking, and evidence under `retrieval/`;
- document-to-graph mapping under `graph/`.

Inject connector/parser/model/repository contracts. Production stages should preserve provenance and permissions, thread tenant/request context, expose bounded concurrency, and make partial progress observable. Do not import a concrete provider subpackage into an engine stage.

## Runtime services

Put configuration loading, provider composition, checkpoint coordination, and
durable workflow implementation in `harborrag-runtime`. Reuse the core job,
repository, lifecycle, and observation ports instead of adding runtime-owned
copies.

The existing connector/parser catalogs demonstrate strict versioning and environment references. A unified composition must retain explicit construction and avoid importing optional providers until selected.

Temporal SDK integration belongs in `harborrag-runtime.temporal`. It must not
become a core, adapter, or engine dependency.

## Application and MCP surfaces

CLI and HTTP code should call `BaseAppService`; MCP tools should call runtime/service interfaces. Neither surface should construct raw provider clients in handlers.

Production interfaces also need stable schemas, exit/error mapping, identity and tenant context, permission enforcement, capability budgets, safe observability, lifecycle handling, and audit recording. Update user documentation only after the command, route, transport, or tool is actually wired.

## Public exports

Add an export to the `harborrag` meta-package only when it is stable, implemented, tested, and documented. Package-local public exports should likewise be intentional and included in import smoke tests.

## Before a pull request

```bash
uv run make lint
uv run make typecheck
uv run make deps-check
uv run make compile
uv run make coverage
```

See [Testing](../testing/README.md) and [CONTRIBUTING.md](../../../CONTRIBUTING.md).
