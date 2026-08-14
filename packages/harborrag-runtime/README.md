# harborrag-runtime

Owns production composition, direct execution, and durable Temporal orchestration
for HarborRAG ingestion. Framework-independent domain types and ports live in
`harborrag-core`; adapters and the engine remain unaware of Temporal.

## Package layout

```text
src/harborrag_runtime/
  __init__.py       # lazy public facade
  contracts.py      # stable SDK request/response value objects
  sdk.py            # SDK lifecycle and service orchestration
  sdk_facades.py    # narrow ingestion/retrieval/graph facades
  sdk_configuration.py
  composition.py    # production resource/repository assembly
  errors.py         # runtime failures
  execution/
    contracts.py    # execution strategy protocols
    submission.py   # shared request-to-source translation
    direct.py       # inline execution strategy
    temporal.py     # durable execution strategy
  retrieval/
    contracts.py    # retrieval ports, policy, and reports
    service.py      # authoritative retrieval use case
    validation.py   # public and projection-boundary validation
  config/
    settings.py     # HARBORRAG_* process settings
    temporal.py     # validated Temporal client/worker configuration
    connectors/     # connector YAML loading and construction
    parsers/        # parser YAML loading and construction
  ingestion/
    composition.py       # runtime lifecycle and stable composition entry point
    runtime_builder.py   # production object-graph builder
    observability.py     # shared ingestion telemetry
    profiles.py          # processing-profile construction
    document/
      models.py          # document release contracts
      service.py         # one-document release application service
      pipeline.py        # ordered, independently retryable stages
      capture.py         # admission and raw/canonical capture
      materialization.py # canonical/chunk/representation materialization
      projection.py      # vector/graph publication
      lifecycle.py       # durable document-version transitions
      normalization.py   # connector normalization strategies
    source/
      models.py          # source request, plan, summary, and outcome contracts
      service.py         # source ingestion facade
      discovery.py       # descriptor discovery and admission planning
      documents.py       # document dispatch result recording
      retry.py           # artifact-first retry service
      finalization.py    # relation repair and removal reconciliation
      plan.py            # immutable source-plan persistence
    maintenance/
      cleanup.py         # projection cleanup
      relation_repair.py # structural relation-repair service
      reindex.py         # connector-free reindexing
      reindex_plan.py    # stale-lane selection policy
  temporal/
    client.py       # source/reindex submission and controls
    schemas.py      # small workflow-history contracts
    ingestion_activities.py   # twelve document-stage activity boundaries
    activity_observability.py
    source_workflow.py    # source, batch, and document workflows
    worker.py       # six-queue worker process
```

The public `harborrag_runtime.sdk` module is a stable facade. Its data
contracts, configuration parsing, execution strategies, and retrieval service
live in focused modules behind that facade. Direct and Temporal strategies use
the same submission builder, so execution mode does not change request
identity or filtering semantics.

Resource lifecycle and runtime observation stay with the runtime components that
own them. The obsolete generic lifecycle port, local job queue, scheduler,
supervisor, runtime-service facade, and runtime-owned test doubles were removed;
the active core repository ports remain the authoritative persistence contracts.

## Temporal ingestion

```text
SourceIngestionWorkflow
  -> SourceBatchWorkflow
       -> DocumentIngestionWorkflow
            fetch -> parse -> canonical -> chunk -> encode
                  -> vector + graph -> verify -> publish
```

Workflow history contains document-version identifiers and MinIO references, not raw
documents, chunk bodies, tables, embeddings, or graph batches. Activities use
Postgres as the publication authority and treat Qdrant and FalkorDB as
rebuildable projections.

The worker builds one production `IngestionRuntime` graph per process from
the configured connector, parser, model, state, object, vector, and graph
settings:

```bash
export HARBORRAG_TEMPORAL_TARGET=localhost:7233
export HARBORRAG_TEMPORAL_NAMESPACE=harborrag
export HARBORRAG_CONNECTOR_CONFIG_PATH=config/connectors.yaml
export HARBORRAG_PARSER_CONFIG_PATH=config/parsers.yaml
export HARBORRAG_MODEL_CONFIG_PATH=config/models.yaml
python -m harborrag_runtime.temporal.worker
```

The normal operator path is the app CLI:

```bash
harborrag ingest start --tenant tenant-1 --connector local --wait
harborrag ingest start --tenant tenant-1 --connector jira --limit 3 --wait
harborrag ingest status RUN_ID --json
harborrag ingest pause RUN_ID
harborrag ingest resume RUN_ID
harborrag ingest cancel RUN_ID
harborrag retrieve "deployment requirements" --tenant tenant-1 --top-k 5 --json
```

`--limit` is a workflow-stable safety bound for operator and acceptance runs;
omitting it retains the configured full-source behavior. Retrieval composes the
same embedding model, active Qdrant collection, FalkorDB graph, and canonical
chunk object store used by ingestion. Query embeddings are marked sensitive
and non-cacheable.

## Public execution facade

`HarborRAG` is an async context manager. Direct execution is the lightweight
default for local development, smoke tests, and embedding HarborRAG as a library;
Temporal execution adds durable submit/status/pause/resume/cancel operations.
Both execute the same engine policies and production repository graph.

```python
from harborrag_core.security import AccessContext
from harborrag_runtime.sdk import HarborRAG, IngestionRequest, RetrievalRequest

access = AccessContext(principal_id="user-1", tenant_id="tenant-1")
async with HarborRAG.from_config("harborrag.yaml") as harbor:
    await harbor.ingestion.run(IngestionRequest(access=access, connector_name="local"))
    response = await harbor.retrieval.search(
        RetrievalRequest(access=access, query="deployment requirements")
    )
```

Installed plugins are discovered only when `discover_plugins: true`. Supported
entry-point groups are connectors, parsers, model providers, vector repositories,
graph repositories, and object stores. Every plugin declares capabilities and an
explicit `register()` operation; importing a module alone never registers it.

## Observability

Every ingestion activity emits an OpenTelemetry span with a controlled stage
name and low-cardinality Prometheus metrics. The Compose worker serves its
process registry on port `9464`; `deploy/prometheus/prometheus.yml` scrapes it
as `harborrag-ingestion-worker`. Metrics cover stage outcomes and durations,
document lifecycle counts, durable artifact bytes, route/evidence chunks,
Temporal retries, projection verification failures, cleanup failures, stale
retrieval candidates, and connector rate-limit waits.

Model telemetry uses the adapters' privacy sanitizer. OpenTelemetry is enabled
for embedding calls; Langfuse remains opt-in through
`HARBORRAG_LANGFUSE_ENABLED=true` and is never attached to canonical
documents, chunk payloads, Qdrant points, or FalkorDB properties.

Applications can also use the framework-owned client directly:

```python
from harborrag_runtime.config.settings import RuntimeSettings
from harborrag_runtime.config.temporal import TemporalRuntimeConfig
from harborrag_runtime.temporal.client import IngestionTemporalClient
from harborrag_runtime.temporal.submission import (
    SourceSubmission,
    build_source_input,
)

settings = RuntimeSettings()
source = build_source_input(
    settings,
    SourceSubmission(
        task_id="sync-2026-07-31",
        tenant_id="tenant-1",
        connector_name="local",
    ),
)
client = await IngestionTemporalClient.connect(TemporalRuntimeConfig.from_settings(settings))
reference = await client.start_ingestion(source)
status = await client.get_status(reference.run_id)
```

For the PostgreSQL-backed local server and worker profile, see
[deployment guide](../../docs/developers/deployment/README.md).

## File configuration

```python
from harborrag_runtime.config import load_connector_catalog, load_parser_catalog

connectors = load_connector_catalog("config/connectors.yaml").build_enabled()
parsers = load_parser_catalog("config/parsers.yaml").build_harbor_parser()
```

## Tests

```bash
uv run pytest packages/harborrag-runtime/tests/unit -q
uv run pytest packages/harborrag-runtime/tests/runtime_ingestion/unit -q
uv run pytest packages/harborrag-runtime/tests/runtime_ingestion/workflows -q
```

The deployed smoke test validates the real Temporal runtime and all projection
stores:

```bash
python packages/harborrag-runtime/tests/runtime_ingestion/smoke/ingestion_flow.py
```
