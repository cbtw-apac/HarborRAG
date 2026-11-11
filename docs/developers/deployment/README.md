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
templates in `env-example/`, with mode `0600`, and mints the local MCP bearer token. Prefer it
to copying templates by hand. Each service reads these files:

| Service | Reads |
| --- | --- |
| Data services (`docker-compose.database.yml`) | `env/.env.database` |
| Temporal server and worker (`docker-compose.temporal.yml`) | `env/.env.database` and `env/.env.temporal`; the worker also receives `env/.env.connector`, `env/.env.parser` and `env/.env.models` |
| API (`docker-compose.yml`) | `env/.env.database`, `env/.env.temporal`, `env/.env.connector`, `env/.env.models` and `env/.env.api`; the container itself receives only `env/.env.api` and `env/.env.models` |
| MCP server (`scripts/deployment/mcp.sh`) | `env/.env.database`, `env/.env.mcp` (required) and `env/.env.models`; from `env/.env.api` only `HARBORRAG_ENV`, so it accepts the reader keys the API's environment issued |
| Host `harborrag` command in a checkout | `env/.env.connector`, `env/.env.parser` and `env/.env.models`; from `env/.env.database` only the MinIO credentials and an uncommented `HARBORRAG_CONTROL_DB_URL` line with its encryption key |

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
- Monitoring uses a separate `env/.env.monitoring`. Create it from
  `env-example/.env.monitoring.example` and set `GRAFANA_ADMIN_PASSWORD`, as in
  [Run the monitoring stack](#run-the-monitoring-stack).
- An `env/.env.monitoring` created from an older template has no
  `HARBORRAG_METRICS_SCRAPE_TOKEN=` line. Add that line from
  `env-example/.env.monitoring.example`, then copy the token into it as in
  [Run the monitoring stack](#run-the-monitoring-stack).

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
inside the Docker network rather than on the host. The FalkorDB browser also defaults to host
port `3000`, which collides with Grafana in the monitoring stack.

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
`env/.env.api` when it is blank; `/metrics` answers only to that bearer token.

The API receives the model credentials from `env/.env.models` for chat completions and
synchronous vector retrieval, while connector credentials stay with the ingestion workers.

`deploy/compose/docker-compose.yml` is the single API composition. It joins the external data
network created by `docker-compose.database.yml` and connects to the Temporal service created
by `docker-compose.temporal.yml`. Environment policy, rather than separate development and
production Compose files, controls the API mode. Production deployments must supply
authentication and their own secret, TLS and network policy.

## Run the monitoring stack

`deploy/compose/docker-compose.monitoring.yml` defines version-pinned Prometheus, Grafana and
Loki services using files under `deploy/prometheus`, `deploy/grafana` and `deploy/loki`.
Prometheus and Grafana publish loopback-only ports by default; Loki is reachable only inside
the private monitoring network.

Prometheus joins `harborrag-data-network` and scrapes the ingestion worker on port `9464` and
the API's ops port `9465`. The API exports request count, latency, in-flight, Python runtime
and process metrics with bounded route template labels. The worker exports bounded stage,
document, artifact, chunk, retry, cleanup, verification, stale-candidate and connector
rate-limiting metrics. Dashboard and alert policy remain deployment-specific.

The API's `/metrics` answers only to the metrics scrape token in `env/.env.api`. Prometheus
reads the same value from `env/.env.monitoring`: the monitoring Compose file turns
`HARBORRAG_METRICS_SCRAPE_TOKEN` into the `api-scrape-token` secret, mounted in Prometheus at
`/run/secrets/api-scrape-token`, and `deploy/prometheus/prometheus.yml` sends it as the bearer
token. Monitoring stays separate from `dev.sh`, which never creates or writes
`env/.env.monitoring`, so you copy the token by hand.

### Monitoring prerequisites

- `scripts/deployment/dev.sh data` has run, so `harborrag-data-network` exists. The monitoring
  stack declares it external.
- `scripts/deployment/dev.sh api` or `dev.sh up` has started the API at least once, so
  `env/.env.api` holds a non-empty `HARBORRAG_METRICS_SCRAPE_TOKEN`. `bootstrap` and `data`
  leave it blank.

### Start the monitoring stack

1. Create the protected monitoring environment file and copy the metrics scrape token into it.
   The value must equal `HARBORRAG_METRICS_SCRAPE_TOKEN` in `env/.env.api`:

   ```bash
   cp env-example/.env.monitoring.example env/.env.monitoring
   chmod 600 env/.env.monitoring
   sed -i "s/^HARBORRAG_METRICS_SCRAPE_TOKEN=.*/$(grep '^HARBORRAG_METRICS_SCRAPE_TOKEN=' env/.env.api)/" env/.env.monitoring
   ```

2. Set `GRAFANA_ADMIN_PASSWORD` in `env/.env.monitoring`. It ships empty and has no
   checked-in fallback, so Compose refuses to start until you set it.
3. Grafana defaults to host port `3000`, which the FalkorDB browser already uses. Change
   `GRAFANA_PORT` or `FALKORDB_BROWSER_PORT`.
4. Start the stack:

   ```bash
   docker compose --env-file env/.env.monitoring \
     --file deploy/compose/docker-compose.monitoring.yml up --detach
   ```

In Prometheus, on port `9090` by default (`PROMETHEUS_PORT`), the `harborrag-api` target is
`up` when the token matches.

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

Prometheus keeps the token it was created with. A plain `up --detach` leaves the running
container and its old token in place, so recreate Prometheus after any token change:

```bash
docker compose --env-file env/.env.monitoring \
  --file deploy/compose/docker-compose.monitoring.yml up --detach --force-recreate prometheus
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
and is not inside a checkout, so without one the command stops with
`harborrag: no project found. …` and exit 1. Mount a directory that holds a
`harborrag.yaml`, which may be empty, and name it with `HARBORRAG_PROJECT`; the image's
`HARBORRAG_*_CONFIG_PATH` variables keep the catalogs at `/app/config`.

A command that calls an operation also needs the service settings: `HARBORRAG_CONTROL_DB_URL`,
`HARBORRAG_SECRETS_ENCRYPTION_KEY` and the store endpoints the container can reach, here in
an environment file of your own, `cli.env`, and `env/.env.models` for chat:

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
and database settings and network-reachable data services. Its writable JSONL audit path is
`/var/lib/harborrag/.harborrag/mcp-audit.jsonl`.

Use `scripts/deployment/mcp.sh --http` for the authenticated loopback HTTP transport and the
local status and configuration UI. Remote MCP needs TLS and a production JWT/JWKS verifier,
and the local container command intentionally does not provide it.

The Explorer MCP UI server is optional and separate. `scripts/deployment/mcp-ui.sh` builds
`deploy/docker/Dockerfile.mcp-ui`, a layer over the MCP image that adds the `ui` extra and runs
`harborrag-mcp-ui`, and starts it from `deploy/compose/docker-compose.mcp-ui.yml` on port
`8011`. Its audit path is the same, inside its own `harborrag-mcp-ui-audit` volume. The MCP
image and `mcp.sh` never include or start it.

## Model download and warmup scripts

`scripts/models/` contains helpers for Docling and FastEmbed downloads, warmup and local-model
smoke checks. Inspect each script before use: provider and model downloads may need network
access, substantial disk space and platform-specific runtimes. Keep caches outside container
layers when they need an independent lifecycle.

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
