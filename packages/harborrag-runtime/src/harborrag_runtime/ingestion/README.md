# Ingestion runtime structure

Contributor notes for `harborrag_runtime.ingestion`, grouped by application
responsibility:

- `document/` owns one document release and its durable stage boundaries.
- `source/` owns discovery, bounded dispatch, retry and source finalization.
- `maintenance/` owns operations over already-published data: cleanup, relation repair,
  connector-free reindexing and the retention of retired document versions.
- The package root owns cross-cutting composition, processing profiles, connection
  resolution and telemetry.

`harborrag_runtime.ingestion` is the stable facade. Callers import public application
services and contracts from it; nested modules are implementation details for the runtime
and white-box tests.

## Dependency direction

```text
document  <- source
document  <- maintenance
maintenance <- source finalization

composition/runtime_builder -> document + source + maintenance
```

Maintenance uses structural protocols when it needs only part of a source contract. That
keeps maintenance services independent of source orchestrators and package
initialization acyclic.

## Add a source

Connector-aware canonical normalization is composed under `document/normalizers/`, but
provider behaviour is not implemented there. A connector that needs its own transform
(Confluence, Jira and local do) owns a `document_transform.py` and exposes the factory as
`document_transform_factory` on its `ConnectorProviderDefinition`. The runtime discovers
those factories and bridges them to the generic engine `DocumentNormalizer`; its router
knows only the `source_system` key and `BaseDocumentNormalizer`. Integrations can still
extend `SourceDocumentNormalizerBuilder` for application-local strategies.

A new source enters production through the composition root. Pass an extended
`SourceDocumentNormalizerBuilder`, a `ChunkingConfig` with the new source-to-profile
mapping, and any extra `ChunkStrategy` instances to `IngestionRuntimeBuilder` or
`build_ingestion_runtime`. A strategy may expose a `record_validator` callable, which the
chunking registry calls without adding provider branches to shared validation. Connector
config factories, constructor dependencies, path fields, aliases and default public
document kinds belong to the connector's `ConnectorProviderDefinition` registration, so a
new source needs no edits in generic API, configuration or engine modules. Writing the
strategy itself is covered in the
[chunking notes](../../../../harborrag-engine/src/harborrag_engine/ingestion/chunking/README.md).

## Scheduled ingestion

Recurring ingestion lives in `harborrag_runtime.scheduling`. `ScheduleService` validates
definitions and manages creation, updates, pause and unpause, manual triggers, deletion,
inspection and backfill through `temporal.schedules.TemporalScheduleBackend`. Each firing
starts the source-ingestion workflow with a fresh task ID.

At startup the application syncs `config/sources/schedules.yaml` into the config-owned
schedules. Updating an existing schedule keeps an operator's pause state and note; a
changed `paused` value in the file is applied. Schedules created through `ScheduleService` with owner `api` are left alone,
and `ScheduleService` refuses API changes to a config-owned schedule. The control plane's
`Connection.schedule` field plays no part in this reconciliation.

What operators declare and see (overlap policies, pause handling, skipped firings) is in
[Scheduled ingestion](../../../../../docs/users/scheduled-ingestion.md).

## Tests

Tests mirror these boundaries under `packages/harborrag-runtime/tests/runtime_ingestion/`:

- `fixtures/` holds the shared fakes.
- `unit/document`, `unit/source`, `unit/maintenance`, `unit/temporal`,
  `unit/scheduling` and `unit/composition` hold the fast behaviour tests.
- `workflows/` holds the deterministic Temporal replay tests.
- `smoke/` holds the opt-in deployed smoke checks; see its
  [README](../../../tests/runtime_ingestion/smoke/README.md).
