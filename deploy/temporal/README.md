# Temporal test configuration

HarborRAG loads runtime Temporal settings from `config/temporal.yaml`. The API,
CLI, and worker images set `HARBORRAG_TEMPORAL_CONFIG_PATH` to
`/app/config/temporal.yaml`. Compose mounts this file read-only into the worker
and mounts the full `config/` directory into the API. Restart both processes
after editing it; an image rebuild is not required:

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
Compose. Use `config/temporal.example.yaml` as the annotated reference. Keep
`HARBORRAG_TEMPORAL_API_KEY` and other secrets in the ignored
`env/.env.temporal` file or a secret manager, never in either YAML file.

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

- REST ingestion routes are under `/v1/ingestions`; cancellation is
  `POST /v1/ingestions/{task_id}/cancel` and failed-only retry is
  `POST /v1/ingestions/{task_id}/retry-failures`.
- Public API success values are `SUCCESS`, and partial success is `PARTIAL`;
  `Completed`, `Succeeded`, and `CompletedWithErrors` are not public values.
- A create request selects a configured `connection_id`; it does not accept an
  arbitrary caller-provided `source_id`.
- Pause and resume are currently Temporal/CLI controls; the REST router does
  not expose pause/resume endpoints.
- Vector and graph projection writes are separate idempotent activities,
  followed by verification. A test requiring a single atomic `index_upsert`
  activity describes a different implementation contract.

## Local stack

Create the ignored environment files, then start the data services, Temporal,
worker, and API:

```bash
scripts/deployment/dev.sh init
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

Python source is baked into the worker image, while only the YAML file is bind
mounted. Rebuild after a worker-code change, then verify queue polling:

```bash
scripts/deployment/dev.sh worker --build
docker compose \
  --env-file env/.env.database \
  --env-file env/.env.temporal \
  --file deploy/compose/docker-compose.temporal.yml \
  --profile worker logs --since 2m temporal-worker
```
