# harborrag-runtime

Composes [HarborRAG](https://github.com/cbtw-apac/HarborRAG) for production: it builds the
repositories, model clients and engine policies from settings, and runs ingestion either
inline or durably on Temporal. It also hosts the SDK facade, retrieval, chat and memory
composition, schedules and topology enrichment. Domain types and ports live in
`harborrag-core`; adapters and the engine stay unaware of Temporal.

To use HarborRAG as a library, install the [`harborrag`](https://pypi.org/project/harborrag/)
facade instead, which re-exports this package's SDK.

## Install

```bash
pip install "harborrag-runtime[temporal]"
```

The `temporal` extra adds the Temporal client that durable ingestion needs. `reader` adds
the clients retrieval needs, `production` the clients the ingestion worker needs, `chat`
the model client and `memory` the `harborrag-memory` package.

## Find your way around the package

```text
src/harborrag_runtime/
  sdk/                      # stable SDK facade: HarborRAG, its configuration and service facades
  execution/                # direct and durable execution strategies, source submission, task registry
  temporal/                 # workflows, activities, client, gateway, schedule backend and the worker
  ingestion/                # document release, source orchestration and maintenance services
  retrieval/                # authoritative dense, sparse, hybrid and graph retrieval, and reranking
  chat/                     # chat service, model-client composition and per-tenant chat clients
  memory/                   # database-backed conversation memory and its composition
  scheduling/               # schedule management on top of the execution engine
  topology/                 # opt-in durable topology enrichment, independent of publication
  composition/              # control-plane repositories, MCP reader composition, storage factories
  config/                   # HARBORRAG_* settings, Temporal configuration, connector and parser catalogs
  config_store/             # runtime access to settings stored in the control database
  connections/              # connection services shared by the API and the workers
  security/                 # operator-side security operations
  observability/            # telemetry shared by reader and ingestion compositions
  events/                   # runtime event contracts and publication
  ingestion_contracts.py    # provider-neutral durable ingestion gateway and DTOs
  contracts.py              # stable request and response contracts of the SDK
  reader_contracts.py       # compatibility exports for reader contracts
  source_query.py           # secret-free source query and processing inputs
  document_stage_catalog.py # sandbox-safe catalog of document stages
  temporal_models.py        # replay-safe routing and retry models
  plugins.py                # entry-point plugin discovery and registration
  rate_limiting.py          # connector rate-limit coordination
  serialization.py          # deterministic JSON conversion of runtime values
  tokenization.py           # provider-neutral token estimates
  errors.py                 # runtime and Temporal boundary errors
```

The ingestion services and their dependency rules are described in the
[ingestion runtime notes](https://github.com/cbtw-apac/HarborRAG/blob/main/packages/harborrag-runtime/src/harborrag_runtime/ingestion/README.md),
and chat composition in the
[runtime chat notes](https://github.com/cbtw-apac/HarborRAG/blob/main/packages/harborrag-runtime/src/harborrag_runtime/chat/README.md).

The public `harborrag_runtime.sdk` module is a stable facade. Its data contracts,
configuration parsing, execution strategies and retrieval service live in focused modules
behind it. Direct and Temporal strategies use the same submission builder, so the
execution mode does not change request identity or filtering semantics.

Applications submit durable ingestion through `ingestion_contracts.IngestionGateway`. Its
submission, retry, status and result DTOs hold no Temporal replay state or workflow
options; `temporal.gateway.TemporalIngestionGateway` translates them to Temporal payloads
and back. Connector preparation and task registration live in
`execution/source_submission.py` and `execution/task_registry.py`, so changing the
execution provider does not change the application's ingestion services. The application
composition takes a gateway factory that receives `RuntimeSettings`, plus a separate
provider description for health reporting. The default composition turns the Temporal YAML
policy into `SourceSubmissionDefaults`; another composition can call
`prepare_source_submission` with its own defaults without loading Temporal configuration.

`document_stage_catalog.py` and `temporal_models.py` stay lightweight top-level modules on
purpose. Temporal imports them inside its restricted workflow sandbox, so placing them
below provider-owning packages would run adapter initialization during workflow
registration.

Resource lifecycle and runtime observation stay with the components that own them. There
is no generic lifecycle port, local job queue or supervisor: durable orchestration,
including recurring schedules, runs on Temporal, and the core repository ports in
`harborrag-core` are the authoritative persistence contracts.

## Run ingestion durably on Temporal

```text
SourceIngestionWorkflow
  -> SourceBatchWorkflow
       -> DocumentIngestionWorkflow
            fetch -> parse -> canonical -> chunk -> encode
                  -> vector + graph -> verify -> publish
```

Workflow history holds document-version identifiers and object-store references, never
raw documents, chunk bodies, tables, embeddings or graph batches. Activities treat
Postgres as the publication authority and Qdrant and FalkorDB as rebuildable projections.

The worker builds one production `IngestionRuntime` per process from the configured
connector, parser, model, state, object, vector and graph settings, and polls the task
queues named in `config/temporal.yaml`. Its configuration files default to the paths
below; the variables override them:

```bash
export HARBORRAG_TEMPORAL_CONFIG_PATH=config/temporal.yaml
export HARBORRAG_CONNECTOR_CONFIG_PATH=config/sources/connectors.yaml
export HARBORRAG_PARSER_CONFIG_PATH=config/ingestion/parsers.yaml
export HARBORRAG_MODEL_CONFIG_PATH=config/models/models.yaml
python -m harborrag_runtime.temporal.worker
```

The worker images run the same module. In the local stack, `scripts/deployment/dev.sh`
starts the worker in Docker; see the
[deployment guide](https://github.com/cbtw-apac/HarborRAG/blob/main/docs/developers/deployment/README.md).

The Temporal YAML owns connection defaults, task queues, retry budgets, worker capacity and
timeouts. Environment values such as `HARBORRAG_TEMPORAL_TARGET` override them per
deployment. Every supported field is listed in
[Temporal configuration](https://github.com/cbtw-apac/HarborRAG/blob/main/deploy/temporal/README.md).
Keep `HARBORRAG_TEMPORAL_API_KEY` in `env/.env.temporal` or a secret manager.

Operators drive the same runtime with the `harborrag ingestions` commands of a repository
checkout. How they name a connection and follow a run is in the
[CLI reference](https://github.com/cbtw-apac/HarborRAG/blob/main/docs/users/cli-reference/README.md#run-and-control-ingestion).

Applications that bypass the SDK can submit to Temporal with
`temporal.client.IngestionTemporalClient`. `temporal.submission.build_source_input(settings,
submission, connection=...)` needs the `ResolvedConnection` that
`ingestion.connections.ConnectionResolver.resolve(tenant_id=..., source_key=...)` returns;
resolving a connection also saves a connection revision to the control database. The
client's `start_ingestion` returns a reference whose `run_id` is the task ID that
`get_status` takes.

## Use the SDK from Python

`harborrag_runtime.sdk.HarborRAG`, re-exported by the `harborrag` package, is an async
context manager. Direct execution is the lightweight default for local development, smoke
tests and embedding HarborRAG as a library; Temporal execution adds durable `submit`,
`status`, `pause`, `resume` and `cancel`. Both run the same engine policies and production
repository graph. Examples and the settings the SDK reads are in the
[Python SDK guide](https://github.com/cbtw-apac/HarborRAG/blob/main/docs/users/python-sdk/README.md).

Installed plugins are discovered only with `discover_plugins: true`. The entry-point
groups are `harborrag.connectors`, `harborrag.parsers`, `harborrag.model_providers`,
`harborrag.vector_repositories`, `harborrag.graph_repositories` and
`harborrag.object_stores`. Every plugin declares its capabilities and an explicit
`register()` operation; importing a module alone never registers it.

## Retrieve and rerank

Retrieval composes the same embedding model, active Qdrant collection, FalkorDB graph and
canonical chunk object store that ingestion writes. Query embeddings are marked sensitive
and non-cacheable.

`retrieval/reranking.py` holds the rerank stage. `PassageReranker` orders passages the
caller may already read, using the client built from the `rerank` section of the model
catalog. It puts the model's score in `metadata["rerank_score"]` and leaves `relevance`
at the lane's similarity. Passage text is sent to the rerank provider as sensitive and
non-cacheable.

## Monitor ingestion

Every ingestion activity emits an OpenTelemetry span with a controlled stage name, plus
low-cardinality Prometheus metrics. The Compose worker serves its process registry on port
`9464`, and `deploy/prometheus/prometheus.yml` scrapes it as
`harborrag-ingestion-worker`. The metrics cover stage outcomes and durations, document
lifecycle counts, durable artifact bytes, route and evidence chunks, Temporal retries,
projection verification failures, cleanup failures, stale retrieval candidates and
connector rate-limit waits.

Model telemetry uses the adapters' privacy sanitizer. OpenTelemetry is on for embedding
calls; Langfuse is opt-in with `HARBORRAG_LANGFUSE_ENABLED=true` and is never attached to
canonical documents, chunk payloads, Qdrant points or FalkorDB properties.

## Load the connector and parser catalogs

```python
from harborrag_runtime.config import load_connector_catalog, load_parser_catalog

connectors = load_connector_catalog("config/sources/connectors.yaml").build_enabled()
parsers = load_parser_catalog("config/ingestion/parsers.yaml").build_harbor_parser()
```

`build_enabled()` raises `ConnectorConfigurationError` until every environment variable
the enabled connectors reference is set, such as `LOCAL_SOURCE_PATH` or
`CONFLUENCE_BASE_URL` from `env/.env.connector`.

## Contributing

See [CONTRIBUTING](https://github.com/cbtw-apac/HarborRAG/blob/main/CONTRIBUTING.md).
Tests for this package live in `packages/harborrag-runtime/tests/`. Run them from the
repository root:

```bash
uv run pytest packages/harborrag-runtime/tests
```

The deployed ingestion smoke test checks the real Temporal runtime and every projection
store. It flushes the configured Redis database, so read its
[README](https://github.com/cbtw-apac/HarborRAG/blob/main/packages/harborrag-runtime/tests/runtime_ingestion/smoke/README.md)
before you run it.

## License

Apache License 2.0.
