# harborrag-runtime

Owns production composition and durable Temporal orchestration for HarborRAG
ingestion. Framework-independent domain types and ports live in
`harborrag-core`; adapters and the engine remain unaware of Temporal.

## Package layout

```text
src/harborrag_runtime/
  __init__.py       # lazy public facade
  composition.py    # production resource/repository assembly
  errors.py         # runtime failures
  config/
    settings.py     # HARBORRAG_* process settings
    temporal.py     # validated Temporal client/worker configuration
    connectors/     # connector YAML loading and construction
    parsers/        # parser YAML loading and construction
  temporal/
    client.py       # ingestion submission and workflow controls
    dependencies.py # worker-owned service graph and durable state boundary
    lifecycle.py    # client/dependency ownership
    models.py       # versioned workflow-history payloads
    activities/     # non-deterministic I/O and engine work
    workflows/      # deterministic orchestration
    workers/        # queue registration and worker groups
    worker.py       # process entry point
```

The generic async-lifecycle and observer interfaces are in
`harborrag_core.ports.runtime`. The obsolete local job store, scheduler,
supervisor, runtime-service facade, and runtime-owned test doubles were removed;
the core job and repository ports are the authoritative contracts.

## Temporal ingestion

```text
IngestionRunWorkflow
  -> IngestionPartitionWorkflow
       -> ArtifactIngestionWorkflow
            preflight -> fetch -> parse -> chunk -> index -> validate -> finalize
```

Workflow history contains versioned identifiers and durable storage references,
not raw documents, chunk bodies, or embeddings. Activities load and persist
large values through `RuntimeIngestionState`, delegate chunking to the engine,
and delegate indexing to `IndexingService`.

The worker builds one production `RuntimeDependencies` graph per process from
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

`HARBORRAG_TEMPORAL_DEPENDENCY_PROVIDER=module:callable` remains an optional
override for deployments that need a custom service graph.

The normal operator path is the app CLI:

```bash
harbor ingest start --tenant tenant-1 --connector local-docs --wait
harbor ingest status RUN_ID --json
harbor ingest pause RUN_ID
harbor ingest resume RUN_ID
harbor ingest retry RUN_ID --artifact ARTIFACT_ID
harbor ingest cancel RUN_ID
```

Applications can also use the framework-owned client directly:

```python
from harborrag_runtime import RuntimeLifecycle
from harborrag_runtime.temporal.models import IngestionRunInput

runtime = await RuntimeLifecycle.open(config, dependencies)
reference = await runtime.client.start_ingestion(
    IngestionRunInput(
        run_id="sync-2026-07-22",
        tenant_id="tenant-1",
        connector_name="docs",
        manifest_id="manifest-1",
        generation_id="generation-1",
        options=config.workflow_options(),
    )
)
status = await runtime.client.get_status(reference.run_id)
await runtime.close()
```

For the PostgreSQL-backed local server and worker profile, see
[`deploy/temporal/README.md`](../../deploy/temporal/README.md).

## File configuration

```python
from harborrag_runtime.config import load_connector_catalog, load_parser_catalog

connectors = load_connector_catalog("config/connectors.yaml").build_enabled()
parsers = load_parser_catalog("config/parsers.yaml").build_harbor_parser()
```

## Tests

```bash
uv run pytest packages/harborrag-runtime/tests/unit -q
uv run pytest packages/harborrag-runtime/tests/workflows -q
```

The optional Temporal integration test requires
`HARBORRAG_TEMPORAL_TEST_SERVER`:

```bash
uv run pytest packages/harborrag-runtime/tests/integration/test_temporal_runtime_smoke.py -q
```
