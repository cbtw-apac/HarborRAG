# Ingestion runtime structure

The ingestion runtime is grouped by application responsibility:

- `document/` owns one document release and its durable stage boundaries.
- `source/` owns discovery, bounded dispatch, retry, and source finalization.
- `maintenance/` owns operations over already-published data: cleanup, relation
  repair, and connector-free reindexing.
- The package root owns cross-cutting composition, processing profiles, and
  telemetry.

Dependency direction is deliberately narrow:

```text
document  <- source
document  <- maintenance
maintenance <- source finalization

composition/runtime_builder -> document + source + maintenance
```

Maintenance uses structural protocols when it only needs part of a source
contract. This prevents maintenance services from depending on source
orchestrators and keeps package initialization acyclic.

Connector-aware canonical normalization is composed under
`document/normalizers/`, but provider behavior is not implemented there. Each
connector owns its `document_transform.py` and exposes the factory through its
`ConnectorProviderDefinition`. Runtime discovers those factories and bridges
them to the generic engine `DocumentNormalizer`; its router knows only the
`source_system` key and `BaseDocumentNormalizer`. Integrations may still extend
`SourceDocumentNormalizerBuilder` for application-local strategies.

Source extensions enter production through the composition root. Pass an
extended `SourceDocumentNormalizerBuilder`, a `ChunkingConfig` with the new
source-to-profile mapping, and any additional `ChunkStrategy` instances to
`IngestionRuntimeBuilder` (or `build_ingestion_runtime`). A strategy may expose
a `record_validator` callable; the chunking registry invokes it without adding
provider branches to shared validation. Connector config factories,
constructor dependencies, path fields, aliases, and default public document
kinds belong to that connector's `ConnectorProviderDefinition` registration.
The same registration owns its optional `document_transform_factory`.
This keeps a new source from requiring edits in generic API, configuration, or
engine modules.

`harborrag_runtime.ingestion` is the stable façade. Callers should prefer its
exports for public application services and contracts; nested modules are for
runtime and white-box test implementation details.

Tests mirror the same boundaries under `packages/harborrag-runtime/tests/runtime_ingestion/`: shared fakes are in
`fixtures/`, fast behavior tests are grouped under `unit/document`,
`unit/source`, `unit/maintenance`, and `unit/temporal`, while deterministic
Temporal replay tests live in `workflows/`.
