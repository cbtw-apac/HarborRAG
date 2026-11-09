# Temporal test configuration

HarborRAG loads runtime Temporal settings from `config/temporal.yaml`. The API,
CLI, and worker images set `HARBORRAG_TEMPORAL_CONFIG_PATH` to
`/app/config/temporal.yaml`. Compose mounts the whole `config/` directory
read-only into the API, the worker and the MCP server. Restart the API and the
worker after editing it; an image rebuild is not required:

```bash
docker compose \
  --env-file env/.env.database \
  --env-file env/.env.temporal \
  --file deploy/compose/docker-compose.temporal.yml \
  --profile worker restart temporal-worker
docker compose \
  --env-file env/.env.database \
  --env-file env/.env.temporal \
  --file deploy/compose/docker-compose.yml restart api
```

Host-side commands load `config/temporal.yaml` relative to the current working
directory. Configuration precedence is explicit `HARBORRAG_TEMPORAL_*`
environment values, then `config/temporal.yaml`, then built-in defaults. This
allows the same file to use `localhost:7233` on the host and `temporal:7233` in
Compose. Keep `HARBORRAG_TEMPORAL_API_KEY` and other secrets in the ignored
`env/.env.temporal` file or a secret manager, never in the YAML file.

## Settings reference

- `connection.target` is the gRPC frontend as a plain `host:port`. Host
  commands use `localhost:7233`; Compose overrides it with `temporal:7233`.
- `connection.identity` and `worker.identity` are the client and worker
  identities shown in the event history. `null` lets the SDK choose one.
- `connection.allow_insecure_remote` is a development-only acknowledgement:
  without it a plaintext connection to a remote host is rejected. Production
  and Temporal Cloud use `tls`, where `tls.domain` overrides the TLS server
  name and `null` uses the target host.
- `worker.max_concurrent_activities` applies to each of the six task-queue
  workers in a process, so one process runs up to six times that many
  activities and as many executor threads. Keep that product at or below
  `HARBORRAG_CONTROL_DB_POOL_SIZE` + `HARBORRAG_CONTROL_DB_MAX_OVERFLOW`, or the
  worker refuses to start.
- Size `worker.max_concurrent_activities` to the host's CPU and memory, not to
  I/O latency. On a shared 4-vCPU, 15 GB host a document costs about 3.7
  CPU-seconds, so throughput tops out near one document per second and extra
  slots add only queueing and memory. A value of 12 (72 slots and threads per
  process, 216 across three replicas) drove that host into memory thrash on
  2026-10-01 at about 32 s per document. The shipped 4 gives 24 slots per
  process and 8 parser slots across two replicas, which keeps LiteParse OCR
  fan-out (`num_workers: 3` in `config/ingestion/parsers.yaml`) at 24 requests,
  under the OCR server's in-flight ceiling of 32.
- `timeouts.discovery_seconds` and `timeouts.finalization_seconds` are
  per-attempt budgets (60 s to 7 days) of the source-level activities whose
  work grows with the source. Discovery resumes each retry from its last
  persisted page, so the budget bounds one attempt, not the whole listing. Both
  activities heartbeat, so a dead worker is caught within two minutes. Defaults
  when omitted: discovery 1800, finalization 900.
- `maximum_attempts` counts the first execution: 8 means one attempt and at
  most seven retries.
- `retries.document` gives about 23 minutes of retries (2 s doubling to a
  five-minute cap). Storage, database and network errors are retried in every
  stage, so this rides out a MinIO, Postgres or OCR outage; at 5 attempts any
  outage over about 30 s failed the document for good. A longer outage trips the
  batch circuit breaker instead.
- `ingestion.batch_size` is the number of documents per `SourceBatchWorkflow`
  child and `ingestion.document_concurrency` the number of concurrent
  `DocumentIngestionWorkflow` children per wave. Per-document latency is roughly
  `document_concurrency` divided by throughput.
- `workflow.execution_timeout_seconds` is 2592000, 30 days.

## Settings useful to the ingestion test plan

- `worker` controls per-process activity/workflow concurrency and polling. Set
  low concurrency when a restart, pause, or cancellation test needs a reliably
  observable in-progress window.
- `task_queues` names all six queues registered by each worker. These names are
  used by clients, workers, child workflows, metrics, logs, and the Temporal UI.
- `retries.discovery` controls the source/discovery budget. The checked-in
  value of `maximum_attempts: 8` supports the retry-exhaustion case.
- `retries.document` controls fetch, parse, transform, model, index, publish,
  cleanup, selective-retry, and reindex activity retries.
- `workflow` controls root workflow execution/task timeouts, while `health`
  controls the API/CLI Temporal readiness deadline.

Queue and retry settings are copied into every root workflow input. Child
workflows inherit that snapshot, so changing the YAML cannot make an already
running workflow nondeterministic during replay after a worker restart.
Do not rename queues while runs are open. Those histories retain the old names
and require workers polling the old queues; drain them first or temporarily run
workers for both old and new queue sets during a controlled migration.

The configuration deliberately does not expose fault-injection switches.
Transient failures, partial index writes, permanent connector errors, and
corrupt parser inputs should be introduced by test doubles or dedicated test
fixtures so production deployments cannot enable them accidentally.

Before automating the proposed test plan, align it with the current public
contract:

- REST ingestion routes are under `/v1/admin/ingestions`; cancellation is
  `POST /v1/admin/ingestions/{ingestion_id}/cancel` and failed-only retry is
  `POST /v1/admin/ingestions/{ingestion_id}/retry-failures`.
- Public API success values are `SUCCESS`, and partial success is `PARTIAL`;
  `Completed`, `Succeeded`, and `CompletedWithErrors` are not public values.
- A start request names a stored connection by `project_id` and
  `connection_id`; it does not accept an arbitrary caller-provided `source_id`.
- Pause and resume are available through the CLI, SDK, and
  `POST /v1/admin/ingestions/{ingestion_id}/pause|resume`. They take effect at safe batch
  boundaries. HarborRAG pauses a run by signal only, so the Temporal UI shows a
  paused run as `Running`: Temporal 1.31 never dispatches a workflow task that
  was scheduled when a native pause landed, which would strand the run.
  `frontend.WorkflowPauseEnabled` stays on so that resume and cancel can
  natively unpause a run an earlier release paused. Do not pause a HarborRAG
  run from the Temporal UI or CLI on 1.31.x: that native pause can strand it
  the same way. Native pause is safe only on Temporal 1.32 or later together
  with `temporalio` 1.35 or later.
- Vector and graph projection writes are separate idempotent activities,
  followed by verification. A test requiring a single atomic `index_upsert`
  activity describes a different implementation contract.

## Local stack

Create the ignored environment files, then start the data services, Temporal,
worker, and API:

```bash
scripts/deployment/dev.sh bootstrap
scripts/deployment/dev.sh data
scripts/deployment/dev.sh temporal
scripts/deployment/dev.sh worker --build
scripts/deployment/dev.sh api --build
```

The Temporal frontend is published on `localhost:7233` and the UI on
<http://localhost:8080> by default. The Local connector reads the directory set
by `LOCAL_SOURCE_PATH` in `env/.env.connector`.

## Connection troubleshooting

`InvalidMessage(InvalidContentType)` means the SDK did not complete a Temporal
gRPC handshake. Confirm that `HARBORRAG_TEMPORAL_TARGET` is a plain `host:port`
authority for the gRPC frontend (`temporal:7233` inside Compose or
`localhost:7233` on the host), not an `http(s)://` URL or the Web UI on port
8080. Local Compose uses plaintext; a secured remote endpoint must enable TLS
in `config/temporal.yaml` and may read its API key from `env/.env.temporal`.

Python source is baked into the worker image, while `config/` is bind
mounted. Rebuild after a worker-code change, then verify queue polling:

```bash
scripts/deployment/dev.sh worker --build
docker compose \
  --env-file env/.env.database \
  --env-file env/.env.temporal \
  --file deploy/compose/docker-compose.temporal.yml \
  --profile worker logs --since 2m temporal-worker
```
