# Deployment

Choose the part of the HarborRAG stack you want to run or inspect.

| Goal | Start here |
| --- | --- |
| Start HarborRAG for the first time | [Quick Start](../../getting-started/quick-start.md) |
| Start or rebuild selected services | [Run the API and worker](#run-the-api-and-worker) |
| Check which files a process reads | [Prepare the environment files](#prepare-the-environment-files) |
| Add metrics and dashboards | [Run the monitoring stack](#run-the-monitoring-stack) |
| Build a CLI or MCP image | [Build the CLI and MCP images](#build-the-cli-and-mcp-images) |
| Plan a staging or production deployment | [Production prerequisites](#what-a-production-deployment-still-needs) |

The checked-in Compose files and images run the local development stack. Production
topology and remote MCP exposure need operator-owned deployment and security controls.

## Prepare the environment files

`scripts/deployment/dev.sh bootstrap` creates seven protected files in `env/` from the
templates in `env-example/`, with mode `0600`. It also generates the secrets encryption key,
the local MCP bearer token and the passwords of the MCP server's read-only accounts. Prefer
it to copying templates by hand. Each service reads these files:

| Process | Environment files in `env/` |
| --- | --- |
| Data services | `.env.database` |
| Temporal server | `.env.database`, `.env.temporal` |
| Temporal worker | `.env.database`, `.env.temporal`, `.env.connector`, `.env.parser`, `.env.models` |
| API container | `.env.api`, `.env.models` |
| MCP launcher | `.env.mcp`, optional `.env.models`, selected values from `.env.database` and `.env.api` |
| Host CLI in a checkout | `.env.connector`, `.env.parser`, `.env.models`, selected values from `.env.database` and `.env.api` |

- **API:** Compose also reads `.env.database`, `.env.temporal`, and `.env.connector` to
  set values it passes to the container. The API and worker must use the same control
  database and secrets encryption key.
- **MCP:** The launcher takes the database name and its own read-only Postgres and MinIO
  passwords from `.env.database`. It uses `HARBORRAG_ENV` from `.env.api` so it accepts
  reader keys issued in the same environment.
- **CLI:** From `.env.database` it takes MinIO credentials, the local source root, and an
  uncommented control database URL with its encryption key. From `.env.api` it takes the
  two corpus-access settings and the default tenant. See
  [CLI checkout configuration](../../users/cli-reference/README.md#run-from-a-repository-checkout).

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
  [Read-only database and object-store accounts](../../users/detailed-guides/mcp-server/server-operations.md#read-only-database-and-object-store-accounts).
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

`dev.sh` accepts a checkout-relative `DATABASE_ENV_FILE` override and passes the file to
Docker Compose. Change the example passwords before using the stack outside an isolated
developer machine.

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

Run one API process per deployment so every connected client receives live ingestion events.
The event bus is in memory. You can scale ingestion by adding worker replicas.

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
