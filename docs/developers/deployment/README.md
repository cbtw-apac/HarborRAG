# Deployment

Understand how HarborRAG's local stacks fit together, which service reads which environment
file, how to run the optional monitoring stack and build the CLI and MCP images, and what a
production deployment still needs.

The deployment assets are at mixed maturity. The database Compose file supports local adapter
smoke tests, the Temporal Compose file provides a runnable PostgreSQL-backed development
server, and `scripts/deployment/dev.sh` composes those services with the control-plane API.
The checked-in API, CLI, worker and MCP Dockerfiles are runnable local images. A production
topology and remote MCP exposure still need operator-owned composition and security policy.

To start the stack for the first time, follow the [Quick Start](../../getting-started/quick-start.md);
to start, stop and rebuild one service at a time, see
[Running HarborRAG from a checkout](../checkout-quick-start.md#6-start-the-services).

## Prepare the environment files

`scripts/deployment/dev.sh bootstrap` creates seven protected files in `env/` from the
templates in `env-example/`, with mode `0600`. It also generates the secrets encryption key,
the local MCP bearer token and the passwords of the MCP server's read-only accounts. Prefer
it to copying templates by hand. Each service reads these files:

| Service | Reads |
| --- | --- |
| Data services (`docker-compose.database.yml`) | `env/.env.database` |
| Temporal server and worker (`docker-compose.temporal.yml`) | `env/.env.database` and `env/.env.temporal`; the worker also receives `env/.env.connector`, `env/.env.parser` and `env/.env.models`, and from `env/.env.temporal` only the values its Compose service names, such as `OTEL_EXPORTER_OTLP_ENDPOINT` |
| API (`docker-compose.yml`) | `env/.env.database`, `env/.env.temporal`, `env/.env.connector`, `env/.env.models` and `env/.env.api`; the container itself receives only `env/.env.api` and `env/.env.models` |
| MCP server (`scripts/deployment/mcp.sh`) | `env/.env.mcp` (required) and `env/.env.models`; from `env/.env.database` only `POSTGRES_DB` and the passwords of its read-only accounts, `POSTGRES_MCP_PASSWORD` and `MINIO_MCP_PASSWORD`, never the Postgres owner or the MinIO root credentials; from `env/.env.api` only `HARBORRAG_ENV`, so it accepts the reader keys the API's environment issued |
| Monitoring stack (`scripts/deployment/observability.sh`) | `env/.env.monitoring`; from `env/.env.database` only `POSTGRES_DB` |
| Host `harborrag` command in a checkout | `env/.env.connector`, `env/.env.parser` and `env/.env.models`; from `env/.env.database` only the MinIO credentials, `HARBORRAG_LOCAL_SOURCE_ROOT` and an uncommented `HARBORRAG_CONTROL_DB_URL` line with its encryption key; from `env/.env.api` only `HARBORRAG_CORPUS_ACCESS_MODE`, `HARBORRAG_CORPUS_SHARED_TENANT_ID` and `HARBORRAG_TENANT_ID`, the last as its default tenant |

`HARBORRAG_SECRETS_ENCRYPTION_KEY` in `env/.env.database` encrypts stored connector credentials
in the control database, and the API and the Temporal worker must read the same value. The
template ships it empty, and `dev.sh` generates it on `bootstrap`, `up`, `worker` and `api`
while it is blank and no database volume exists yet. With a blank key and an existing
database volume, `dev.sh` stops with `HARBORRAG_SECRETS_ENCRYPTION_KEY is blank in
env/.env.database, but … already holds a database; restore the key it was written with.`
Outside `dev.sh`, generate the key yourself, or point
`HARBORRAG_SECRETS_ENCRYPTION_KEY_FILE` at a file holding it, such as a Docker or Kubernetes
secret:

```bash
openssl rand -hex 32
```

The API's Compose file refuses to start without the key, and the worker rejects an empty one
before it serves work. Rotating the key after secrets have been stored requires re-encrypting
or re-entering them.

When upgrading an existing checkout:

- Add `HARBORRAG_SECRETS_ENCRYPTION_KEY`, `POSTGRES_PASSWORD` and `MINIO_ROOT_PASSWORD` to the
  existing protected `env/.env.database` before the next start.
- After `scripts/deployment/dev.sh up`, run `scripts/deployment/dev.sh mcp-access` before you
  start the MCP server. It writes `POSTGRES_MCP_PASSWORD` and `MINIO_MCP_PASSWORD` to
  `env/.env.database` when they are blank, and creates or refreshes the read-only accounts the
  server connects with. Run it after every upgrade and after `dev.sh down --volumes`; see
  [Read-only database and object-store accounts](../../users/detailed-guides/mcp-server/setup-and-integration.md#read-only-database-and-object-store-accounts).
- Monitoring uses a separate `env/.env.monitoring`. Create it with
  `scripts/deployment/observability.sh bootstrap`, as in
  [Run the monitoring stack](#run-the-monitoring-stack).
- An `env/.env.monitoring` created from an older template has no
  `HARBORRAG_METRICS_SCRAPE_TOKEN=` line. Add that line from
  `env-example/.env.monitoring.example`, then copy the token into it as in
  [Run the monitoring stack](#run-the-monitoring-stack). Run
  `scripts/deployment/observability.sh bootstrap` too: it adds a generated
  `HARBORRAG_OBSERVER_PASSWORD`, which the stack now requires.

Compose refuses to start an affected service when one of these required values is absent.
[Upgrading](../../users/upgrading.md) lists the other steps each release needs.

## Run the data services

`deploy/compose/docker-compose.database.yml` defines:

- Qdrant on ports `6333` and `6334`
- FalkorDB on ports `6379` and `3000`
- Redis on host port `6380` by default (container port `6379`)
- PostgreSQL on port `5432`
- MinIO on ports `9000` (S3 API) and `9001` (console)

Host port `6379` is FalkorDB, not Redis: Redis is published on `6380` to avoid the collision.
`HARBORRAG_REDIS_URL=redis://redis:6379/0` is still correct, because that URL is resolved
inside the Docker network rather than on the host.

The service image tags are pinned in the Compose file. The environment template controls
ports and credentials. Its `DATABASE_PULL`, `DATABASE_STARTUP_TIMEOUT` and
`DATABASE_COMPOSE_FILE` entries are vestigial and read by nothing.

FalkorDB persists RDB snapshots in its named volume. Do not enable AOF for this service:
FalkorDB 4.20.1 can create and restore the knowledge-graph uniqueness constraint from a
snapshot, but crashes when Redis replays the asynchronous `GRAPH.CONSTRAINT` command from
AOF. The separate Redis service keeps AOF persistence.

`dev.sh` accepts a `DATABASE_ENV_FILE` override and passes the selected file to Docker
Compose. Change the example passwords before using the stack outside an isolated developer
machine.

Every port is published on `HARBORRAG_DATA_BIND_ADDRESS`, `127.0.0.1` by default. Keep it on
loopback: Qdrant, Redis and FalkorDB run without authentication, so anyone who reaches their
ports can read and change the stored data.

The database stack owns the `harborrag-data-network`, and it is the only Compose file that
creates it: every other stack declares it `external: true`. Application containers and the
Temporal worker join it. Temporal control-plane services also sit on their own Compose project
network (`harborrag-temporal_default`). Start the database stack before anything else, or the
other stacks fail on a missing network.

Use the repository smoke runner in
[Testing](../testing/README.md#real-system-smoke-checks) to verify adapters through their
public APIs.

## Run the API and worker

`scripts/deployment/dev.sh` is the entrypoint for the local stack. `up` starts the data
services, Temporal, a Temporal worker and the API, and returns only after the API process
health check succeeds:

```bash
scripts/deployment/dev.sh up
```

Each command owns one part: `temporal` never starts a worker, `worker` starts only the worker,
and `api` starts the API with `--no-deps`. `dev.sh up --no-worker` omits the worker, and
`dev.sh down` stops the topology in reverse order.

`up` and `worker` take `--device cpu|gpu` to choose the CPU or the GPU worker image, and
`down --volumes` passes `--volumes` to Compose, which removes the stack's named volumes.
`dev.sh sharepoint-login [--logout] [--device cpu|gpu]` signs the worker in for a SharePoint
connection with `auth_mode: device_code` and keeps the token in the
`harborrag-sharepoint-tokens` volume; see
[Delegated sign-in](../../users/configuration/connector-config.md#delegated-sign-in).

The API and worker commands reuse Compose's existing local `harborrag-api-api` and
`harborrag-temporal-temporal-worker` images by default. A missing image is built on the first
start. Use `api --build`, `worker --build` or `up --build` after changing application source,
dependency metadata or worker configuration. Docker then reuses the dependency-install layer
while `uv.lock` and the package metadata are unchanged, so source-only rebuilds do not
reinstall libraries.

The API binds to `127.0.0.1:8000` by default. Override `HARBORRAG_API_BIND_ADDRESS` or
`HARBORRAG_API_PORT` when you need another local binding. The local container's internal
wildcard listener is acknowledged by `HARBORRAG_ALLOW_INSECURE_DEV=true`, which is safe only
while the published address stays on loopback. Configure HMAC authentication before using a
non-loopback published address. Authentication and CORS values belong in the ignored
`env/.env.api`.

The API's ops port `9465`, which serves `/metrics`, is published on loopback only. `dev.sh api`
and `dev.sh up` generate the metrics scrape token, `HARBORRAG_METRICS_SCRAPE_TOKEN`, in
`env/.env.api` when it is blank; `/metrics` answers only to that bearer token. Outside
`dev.sh`, a blank token refuses every scrape with `401`.

The API receives the model credentials from `env/.env.models` for chat completions and
synchronous vector retrieval, while connector credentials stay with the ingestion workers.

`deploy/compose/docker-compose.yml` is the single API composition. It joins the external data
network created by `docker-compose.database.yml` and connects to the Temporal service created
by `docker-compose.temporal.yml`. Environment policy, rather than separate development and
production Compose files, controls the API mode. Production deployments must supply
authentication and their own secret, TLS and network policy.

## Run the monitoring stack

`scripts/deployment/observability.sh` runs the optional monitoring stack,
`deploy/compose/docker-compose.monitoring.yml`, with configuration from `deploy/otel-collector`,
`deploy/tempo`, `deploy/loki`, `deploy/prometheus` and `deploy/grafana`. Nothing runs it by
default: `dev.sh` never starts or configures it, and the API, worker and MCP server export no
telemetry until you [turn export on](#turn-telemetry-export-on).

| Service | What it does |
| --- | --- |
| `otel-collector` | Receives OTLP over HTTP at `http://otel-collector:4318` on `harborrag-data-network` and on loopback port `4318` (`OTEL_COLLECTOR_PORT`). Drops every span, span event and log attribute outside an allow-list, masks e-mail addresses and phone numbers in log bodies, turns every span into request, error and duration metrics, then keeps every failed trace, every trace slower than 2 seconds and `HARBORRAG_TRACE_SAMPLE_PERCENT` of the rest (100 locally, about 10 in production). Probes the MCP server's `/healthz` every 30 seconds. |
| `tempo` | Stores the kept traces for 7 days. |
| `loki` | Stores logs for 14 days. |
| `prometheus` | Keeps metrics and their trace exemplars for 30 days. Scrapes the worker on `9464`, the API's ops port `9465`, the collector and the three exporters, and evaluates the alert rules. |
| `alertmanager` | Sends critical alerts to the on-call webhook and warnings to the team webhook. |
| `grafana` | Serves the Prometheus, Loki, Tempo and PostgreSQL data sources, linked so a trace opens its logs and metrics and a log or exemplar opens its trace. Dashboards load into the `Online`, `Offline` and `Shared` folders from [`deploy/grafana/dashboards`](../../../deploy/grafana/dashboards/README.md). |
| `node-exporter` | Host CPU, memory, disk and network. |
| `cadvisor` | CPU, memory and network of each container. |
| `postgres-exporter` | PostgreSQL server and database statistics. |

Only the collector, Prometheus and Grafana publish ports, on
`HARBORRAG_MONITORING_BIND_ADDRESS` (`127.0.0.1` by default); the rest are reachable only inside
the private monitoring network or `harborrag-data-network`. Every container has a memory limit.

The API's `/metrics` answers only to the metrics scrape token in `env/.env.api`. Prometheus
reads the same value from `env/.env.monitoring`: the monitoring Compose file turns
`HARBORRAG_METRICS_SCRAPE_TOKEN` into the `api-scrape-token` secret, mounted in Prometheus at
`/run/secrets/api-scrape-token`, and `deploy/prometheus/prometheus.yml` sends it as the bearer
token. `dev.sh` never writes `env/.env.monitoring`, so you copy the token by hand.

### Monitoring prerequisites

- `scripts/deployment/dev.sh data` has run, so `harborrag-data-network` exists. The monitoring
  stack declares it external.
- `scripts/deployment/dev.sh api` or `dev.sh up` has started the API at least once, so
  `env/.env.api` holds a non-empty `HARBORRAG_METRICS_SCRAPE_TOKEN`. `bootstrap` and `data`
  leave it blank.

### Start the monitoring stack

1. Create the protected monitoring environment file. `bootstrap` copies the template to
   `env/.env.monitoring` with mode `0600` and generates `GRAFANA_ADMIN_PASSWORD` and
   `HARBORRAG_OBSERVER_PASSWORD`; on an existing file it only fills blank passwords:

   ```bash
   scripts/deployment/observability.sh bootstrap
   ```

2. Copy the metrics scrape token into it. The value must equal
   `HARBORRAG_METRICS_SCRAPE_TOKEN` in `env/.env.api`:

   ```bash
   sed -i "s/^HARBORRAG_METRICS_SCRAPE_TOKEN=.*/$(grep '^HARBORRAG_METRICS_SCRAPE_TOKEN=' env/.env.api)/" env/.env.monitoring
   ```

3. Start the stack. `up` prints the Grafana address, port `3030` by default (`GRAFANA_PORT`),
   and the steps to turn export on:

   ```bash
   scripts/deployment/observability.sh up
   ```

4. Let Grafana and postgres-exporter read PostgreSQL. `db-access` creates or refreshes the
   read-only `harborrag_observer` role with `HARBORRAG_OBSERVER_PASSWORD`. The role gets the
   statistics of `pg_monitor` and the run columns of `ingestion_tasks` and
   `task_document_results`, never the request paths, document titles or file names those tables
   also hold. Run it after `dev.sh up` has applied the migrations, and again after every
   upgrade:

   ```bash
   scripts/deployment/observability.sh db-access
   ```

In Prometheus, on port `9090` by default (`PROMETHEUS_PORT`), the `harborrag-api` target is
`up` when the token matches. `observability.sh status` lists the containers,
`logs [SERVICE...]` shows their logs, and `down [--volumes]` stops the stack, with
`--volumes` also deleting its stored data. `MONITORING_ENV_FILE` and `DATABASE_ENV_FILE`
override the env file paths; the launcher reads only `POSTGRES_DB` from the database file.

### Turn telemetry export on

The API, the worker and the MCP server export traces, metrics and logs only when their env
file sets `OTEL_EXPORTER_OTLP_ENDPOINT`. Add this line to `env/.env.api`, `env/.env.temporal`
and `env/.env.mcp`:

```bash
OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4318
```

Then restart the three services so they read it:

```bash
scripts/deployment/dev.sh api
scripts/deployment/dev.sh worker
scripts/deployment/mcp.sh --http
```

Export uses OTLP over HTTP only. A process run from the checkout sends to
`http://127.0.0.1:4318` instead. The worker receives the value through its Compose
environment, so an `OTEL_EXPORTER_OTLP_ENDPOINT` exported in your shell overrides the one in
`env/.env.temporal`. To stop exporting, remove the line and restart the services.

### Dashboards and alerts

Grafana loads five dashboards from [`deploy/grafana/dashboards`](../../../deploy/grafana/dashboards/README.md),
each with fixed UIDs and the `env` and `tenant` variables:

| Folder | Dashboard | Answers |
| --- | --- | --- |
| Online | Online overview | Are MCP and chat healthy right now, and which part is not? |
| Online | MCP and retrieval | Which tool is slow, failing or returning nothing, and which store is behind it? |
| Online | Chat and LLM | Are chat turns fast and grounded, and what do the models cost? |
| Offline | Ingestion and schedules | Did each sync run, what did it change, and which stage or connector failed? |
| Shared | Stores and host | Is a store or the host the bottleneck? |

The ingestion dashboard reads run history from PostgreSQL, so it needs `db-access`.

[`deploy/prometheus/harborrag_alerts.yml`](../../../deploy/prometheus/harborrag_alerts.yml)
holds the alert rules and the thresholds they share with the dashboards. Each alert has a
`severity`, a `side` label and a link to its section in
[Observability runbooks](observability-runbooks.md). To deliver alerts, set the webhook URLs in
`env/.env.monitoring`, then restart the stack:

```bash
HARBORRAG_ALERT_ONCALL_WEBHOOK_URL=<on-call webhook URL>
HARBORRAG_ALERT_TEAM_WEBHOOK_URL=<team webhook URL>
```

Alertmanager posts its standard webhook payload. A chat tool that needs another format takes
a relay, or a receiver in [`deploy/alertmanager/alertmanager.yml`](../../../deploy/alertmanager/alertmanager.yml).
Blank URLs deliver nothing, and alerts still show in Grafana and Prometheus.

Prometheus, Alertmanager and the collector read their configuration only when they start. After
editing a file under `deploy/`, run `observability.sh down` and then `up`. On Docker Desktop, a
container whose single-file mount was replaced by an editor fails to restart until it is
recreated this way.

### Host and container exporters

`node-exporter` shares the host's process namespace and mounts the host's root file system
read-only. `cadvisor` mounts the root file system, `/sys` and `/var/lib/docker` read-only, and
the Docker socket. A read-only mount does not make the socket read-only: whoever controls
`cadvisor` can drive the Docker API, which is root on the host. Both publish no port and sit
only on the private monitoring network. Run them only on a machine where that access is
acceptable. On Docker Desktop, `cadvisor` names containers by cgroup id rather than by name.

### If the API scrape fails

- The `harborrag-api` target is down with
  `unable to read file /run/secrets/api-scrape-token`: `HARBORRAG_METRICS_SCRAPE_TOKEN` in
  `env/.env.monitoring` is blank, usually because it was copied before the API generated the
  token. Rerun the copy step, then recreate Prometheus.
- The `harborrag-api` target is down with `server returned HTTP status 401 Unauthorized`: the
  token in `env/.env.monitoring` differs from the one in `env/.env.api`. Rerun the copy step,
  then recreate Prometheus.
- Compose refuses to start with
  `environment variable "HARBORRAG_METRICS_SCRAPE_TOKEN" required by secret ... is not set`:
  the line is missing from `env/.env.monitoring`. Add `HARBORRAG_METRICS_SCRAPE_TOKEN=` from
  `env-example/.env.monitoring.example`, then rerun the copy step.

Prometheus keeps the token it was created with. `up` leaves the running container and its
old token in place, so restart the stack after any token change. `down` without `--volumes`
keeps the stored data:

```bash
scripts/deployment/observability.sh down
scripts/deployment/observability.sh up
```

To rule out the API side, scrape `/metrics` by hand with the token from `env/.env.api`. A
successful response means the API serves metrics and the copy in `env/.env.monitoring` is the
problem:

```bash
curl --fail -H "Authorization: Bearer $(sed -n 's/^HARBORRAG_METRICS_SCRAPE_TOKEN=//p' env/.env.api)" \
  http://127.0.0.1:9465/metrics
```

### Keep the monitoring stack private

Keep `env/.env.monitoring` protected and monitoring bound to loopback unless an authenticated
TLS reverse proxy and a firewall protect it. The hardened stack initializes a new
`grafana_secure_data` volume, so an older Grafana volume created with the retired
`admin/admin` default is never reused; the old volume remains available for manual recovery
or removal.

## Build the CLI and MCP images

Build the standalone operator images from the repository root:

```bash
docker build -f deploy/docker/Dockerfile.cli -t harborrag-cli .
docker build -f deploy/docker/Dockerfile.mcp -t harborrag-mcp .

docker run --rm harborrag-cli --help
docker run --rm harborrag-mcp --check
```

Both images include the shared runtime, model and retrieval dependencies, the checked-in
configuration and the packaged chat prompts. They run as the non-root `harborrag` user.
Configuration paths inside the images point to `/app/config`; mount a replacement directory
there read-only to deploy a different catalog.

The CLI image uses `harborrag` as its entry point, and its default command is
`doctor -o json`, which runs without a project. Every other command needs a project
directory: the image's working directory, `/var/lib/harborrag`, holds no `harborrag.yaml`
and is not inside a checkout, so without one the command stops with exit 1. Without `-t`,
stdout is not a terminal, so it prints the error envelope with `internal_error` and the
message `no project found. …`; with `-t` or `-o table` it prints
`harborrag: no project found. …`. Mount a directory that holds a
`harborrag.yaml`, which may be empty, and name it with `HARBORRAG_PROJECT`; the image's
`HARBORRAG_*_CONFIG_PATH` variables keep the catalogs at `/app/config`.

A command that calls an operation also needs the service settings: `HARBORRAG_CONTROL_DB_URL`,
`HARBORRAG_SECRETS_ENCRYPTION_KEY` and the store endpoints the container can reach, here in
an environment file of your own, `cli.env`, and `env/.env.models` for chat. The container is
not a checkout, so it reads no corpus-mode lines and `chat` runs on the default `source_acl`;
add `HARBORRAG_CORPUS_ACCESS_MODE` and `HARBORRAG_CORPUS_SHARED_TENANT_ID` to `cli.env`, with
the API's values, to answer from the shared corpus:

```bash
mkdir -p cli-project && touch cli-project/harborrag.yaml
docker run --rm \
  --env-file env/.env.models \
  --env-file cli.env \
  --volume "$PWD/cli-project:/project:ro" \
  --env HARBORRAG_PROJECT=/project \
  harborrag-cli chat "Explain HarborRAG." -o json
```

The MCP image uses `python -m harborrag_mcp_server` as its entry point and defaults to stdio.
An MCP client must keep stdin attached with `-i`; real tool calls also need protected model
settings, a read-only role's database URL and object-store keys, and network-reachable data
services. Its writable JSONL audit path is `/var/lib/harborrag/.harborrag/mcp-audit.jsonl`.

Use `scripts/deployment/mcp.sh --http` for the authenticated loopback HTTP transport and the
local status page. Its container joins `harborrag-data-network`, and Docker publishes the
port on `127.0.0.1` only. The container starts only while `HARBORRAG_ENV` is `dev`, the
Compose default, because its listener beyond loopback and its plaintext links to the data
services are development-only acknowledgements. The wrapper runs
`harborrag-mcp --http --preflight` before it starts the server, so a missing setting is one
line and not a restart loop. Remote MCP needs TLS and a production JWT/JWKS verifier, and
the local container command intentionally does not provide it.

The MCP image records the commit it was built from in its
`org.opencontainers.image.revision` label. The wrapper passes it when it builds, compares it
with the checkout on later runs and says when to rebuild. To stamp a manual build, add
`--build-arg "HARBORRAG_GIT_REVISION=$(git rev-parse HEAD)"`.

The Explorer MCP UI server is optional and separate. `scripts/deployment/mcp-ui.sh` builds
`deploy/docker/Dockerfile.mcp-ui`, a layer over the MCP image that adds the `ui` extra and runs
`harborrag-mcp-ui`, and starts it from `deploy/compose/docker-compose.mcp-ui.yml` on port
`8011`. Its audit path is the same, inside its own `harborrag-mcp-ui-audit` volume. The MCP
image and `mcp.sh` never include or start it.

## Local model smoke script

`scripts/models/smoke_test_local_models.py` sends one chat request, one streamed chat request
and one embedding request to models served by vLLM on your machine, and prints each
response. Its endpoints, `http://localhost:8000/v3` for chat and `http://localhost:8001/v3`
for embeddings, and its model names are fixed in the script; port `8000` is also the API's
default, so edit them to match your servers before you run it.

## Temporal and cloud directories

`deploy/temporal/` holds the Temporal server's dynamic configuration for local development;
the PostgreSQL schema setup and the namespace initialization run as the `temporal-schema` and
`temporal-namespace` services of `deploy/compose/docker-compose.temporal.yml`. None of it is a
production topology. Its README describes HarborRAG's Temporal settings and the TLS settings
for a secured remote endpoint such as Temporal Cloud. `deploy/aws/` reserves cloud deployment
directions and does not provide complete infrastructure-as-code.

## What a production deployment still needs

Before an internet-facing deployment can be considered production-ready, operators still need
deployment-specific authorization policy, TLS and network policy, secret delivery, backup and
restore, resource limits, alert thresholds, and end-to-end tests against their chosen source
systems. Review the [runtime reliability boundary](../architecture/runtime-reliability.md)
before choosing worker, retry, timeout, retention and recovery policy.
