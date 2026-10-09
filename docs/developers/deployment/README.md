# Deployment

Deployment assets are at mixed maturity. The database Compose file supports
local adapter smoke tests, the Temporal Compose file provides a runnable
PostgreSQL-backed development server, and the development helper composes those
services with the control-plane API. The checked-in API, CLI, worker, and MCP
Dockerfiles are runnable local images. Production topology and remote MCP
exposure still require operator-owned
composition and security policy.

## Local repository services

`deploy/compose/docker-compose.database.yml` defines:

- Qdrant on ports 6333/6334;
- FalkorDB on ports 6379/3000;
- Redis on host port 6380 by default (container port 6379);
- PostgreSQL on port 5432;
- MinIO on ports 9000 (S3 API) and 9001 (console).

Note that host port 6379 is **FalkorDB**, not Redis - Redis is published on 6380 to avoid
the collision. Compose builds the worker's `HARBORRAG_REDIS_URL` as
`redis://:${REDIS_PASSWORD}@redis:6379/0`, resolved inside the Docker network rather than
on the host. FalkorDB's browser also defaults to host port 3000, which collides with Grafana
in the monitoring stack; log in to it with `FALKORDB_PASSWORD`.

Every datastore requires a credential from `env/.env.database`:
- Qdrant requires `QDRANT_API_KEY` on every HTTP and gRPC request.
- Redis and FalkorDB require `REDIS_PASSWORD` and `FALKORDB_PASSWORD` (Redis `requirepass`).
- Their healthchecks authenticate through `REDISCLI_AUTH`.

The API, worker and MCP receive these as `HARBORRAG_QDRANT_API_KEY`,
`HARBORRAG_FALKORDB_PASSWORD` and a password-bearing Redis URL. Host-side tools that talk
to the published ports must export the same values. Existing checkouts must delete any
`HARBORRAG_REDIS_URL=redis://redis:6379/0` line from `env/.env.temporal`; `dev.sh worker`
refuses a Redis URL without credentials.

The service image tags are pinned directly in the Compose file. The environment template
controls ports and credentials. Its `DATABASE_PULL`, `DATABASE_STARTUP_TIMEOUT`, and
`DATABASE_COMPOSE_FILE` entries are vestigial and read by nothing.

FalkorDB persists RDB snapshots in its named volume. Do not enable AOF for this
service: FalkorDB 4.20.1 can create and restore the knowledge-graph uniqueness
constraint from a snapshot, but crashes when Redis replays the asynchronous
`GRAPH.CONSTRAINT` command from AOF. The separate Redis service continues to use
AOF persistence.

Prepare protected environment files:

```bash
scripts/deployment/dev.sh bootstrap     # creates all seven env/ files, mode 0600
```

`bootstrap` is preferred over copying templates by hand. It creates the
checkout environment files and mints the local MCP bearer token. It also fills
every empty datastore secret in `env/.env.database` with
`openssl rand -hex 32`: `POSTGRES_PASSWORD`, `MINIO_ROOT_PASSWORD`,
`MINIO_APP_SECRET_KEY`, `QDRANT_API_KEY`, `REDIS_PASSWORD` and `FALKORDB_PASSWORD`. It never replaces a
value that is already set, so it is safe to re-run. MCP reads the database and
model files, plus the MCP file when present; it does not load `env/.env.api`.

`dev.sh data`, `temporal`, `worker`, `api` and `up` refuse to start in two
cases:
- any of those five secrets is empty or still a `change-me`/`REPLACE`
  placeholder;
- `POSTGRES_PASSWORD` or `REDIS_PASSWORD` contains `@ / : # ? %` or whitespace,
  which would corrupt the connection URLs Compose builds from them.

**`HARBORRAG_SECRETS_ENCRYPTION_KEY` in `env/.env.database` ships empty and blocks
startup.** It encrypts stored connector credentials in the control database, and both the
API and the Temporal worker must read the same value. Their launch/runtime validation
guards it and treats an empty value as unset:

```bash
openssl rand -hex 32     # paste into HARBORRAG_SECRETS_ENCRYPTION_KEY
```

Rotating it after secrets have been stored requires re-encrypting or re-entering them.

When upgrading an existing checkout, run `scripts/deployment/dev.sh bootstrap`
once. It appends the missing datastore secrets to the existing
`env/.env.database` without touching values that are already set.

A file that still holds the old `change-me` PostgreSQL password is refused
rather than rotated, because PostgreSQL keeps the password its volume was
initialised with. You have two options:
- change the password inside PostgreSQL, then put the same value in the file. Use the
  interactive `\password` command so the value never appears in a process argument or
  in shell history. Generate it first with `openssl rand -hex 32` and paste it at the
  prompt:

  ```bash
  docker compose --env-file env/.env.database \
    --file deploy/compose/docker-compose.database.yml \
    exec postgres psql -U postgres
  # at the postgres=# prompt:
  #   \password postgres
  ```

- discard the local data with `scripts/deployment/dev.sh down --volumes`, then
  empty the `POSTGRES_PASSWORD=` line so `bootstrap` generates a fresh value.

A `change-me-minio` MinIO password can simply be replaced: MinIO reads its root
credentials on every start.

Monitoring now uses a separate `env/.env.monitoring`; create it from
`env-example/.env.monitoring.example` and set `GRAFANA_ADMIN_PASSWORD`. Compose
intentionally refuses to start an affected service when a required password is
absent.

The unified deployment helper accepts `DATABASE_ENV_FILE` overrides and passes
the selected file to Docker Compose.

The stack owns the stable `harborrag-data-network`, and it is the only compose file that
creates it - every other stack declares it `external: true`. Application containers and the
Temporal worker join it. Temporal control-plane services additionally sit on their own
Compose project network (`harborrag-temporal_default`). Start the database stack before
anything else, or the other stacks fail on a missing network.

Use the repository smoke runner in [Testing](../testing/README.md#real-system-smoke-checks) to verify adapters through their public APIs.

## Monitoring stack

`docker-compose.monitoring.yml` defines version-pinned Prometheus, Grafana, and
Loki services using files under `deploy/prometheus`, `deploy/grafana`, and
`deploy/loki`. Prometheus and Grafana publish loopback-only ports by default;
Loki is reachable only inside the private monitoring network.
Prometheus joins `harborrag-data-network`; API metrics at `/api/v1/metrics`
require an admin bearer token, while the default configuration scrapes
the ingestion worker on port `9464`. The API exports request count,
latency, in-flight, Python runtime, and process metrics using bounded route
template labels. The worker exports bounded stage, document, artifact,
chunk, retry, cleanup, verification, stale-candidate, and connector
rate-limiting metrics. Dashboard and alert policy remain
deployment-specific.

`scripts/deployment/dev.sh bootstrap` does **not** create `env/.env.monitoring`, and this
stack declares `harborrag-data-network` as external, so `dev.sh data` must have run first:

```bash
scripts/deployment/dev.sh data          # creates harborrag-data-network

cp env-example/.env.monitoring.example env/.env.monitoring
chmod 600 env/.env.monitoring
# GRAFANA_ADMIN_PASSWORD ships empty and is guarded, so Compose refuses to start
# until you set it. Grafana also defaults to host port 3000, which the FalkorDB
# browser already uses -- change GRAFANA_PORT or FALKORDB_BROWSER_PORT.
docker compose --env-file env/.env.monitoring \
  --file deploy/compose/docker-compose.monitoring.yml up --detach
```

Set `GRAFANA_ADMIN_PASSWORD` in the protected monitoring environment file
before starting the stack; Compose has no checked-in password fallback.
Monitoring remains separate from `scripts/deployment/dev.sh` bootstrap. Keep
its environment file protected and monitoring bound to loopback unless an
authenticated TLS reverse proxy and firewall protect it. The hardened stack
initializes a new `grafana_secure_data` volume so an older Grafana volume
created with the retired `admin/admin` default is never reused; the old volume
remains available for manual recovery or removal.

## Application Compose files

`scripts/deployment/dev.sh` is the only deployment entrypoint. Run `bootstrap`
once to create protected environment files from `env-example/`, including the
API-only `env/.env.api`, then review the placeholders. The `up` command starts
data services, Temporal, a Temporal worker, and the API, returning only after
the API process health check succeeds:

```bash
scripts/deployment/dev.sh bootstrap
scripts/deployment/dev.sh up
```

When the database and Temporal stacks are already running, start only the API
with:

```bash
scripts/deployment/dev.sh api
```

The API and worker commands reuse Compose's existing local
`harborrag-api-api` and `harborrag-temporal-temporal-worker` images by default.
A missing image is built on the first start. Use `api --build`, `worker
--build`, or `up --build` after changing application source, dependency
metadata, or worker configuration. Docker then reuses the dependency-install
layer while `uv.lock` and package metadata are unchanged, so source-only
rebuilds do not reinstall libraries.

The API binds to `127.0.0.1:8000` by default. Override
`HARBORRAG_API_BIND_ADDRESS` or `HARBORRAG_API_PORT` when another local binding
is required. `env/.env.api` ships with `HARBORRAG_AUTH_MODE=hmac`, and
`bootstrap` generates its `HARBORRAG_AUTH_SECRET`. `dev.sh api` refuses to start
while the secret is empty, a placeholder or shorter than 32 characters. Mint a
short-lived owner token (all tenants; `HARBORRAG_AUTH_MAX_TOKEN_LIFETIME_SECONDS`,
1 hour by default) for the running API and send it as a bearer token:

```bash
export HARBORRAG_API_TOKEN="$(scripts/deployment/dev.sh token)"
curl --fail-with-body --header "Authorization: Bearer $HARBORRAG_API_TOKEN" \
  http://127.0.0.1:8000/v1/connections
```

The token minter runs inside the API container (rebuild with `dev.sh api --build`
the first time). It refuses outside `HARBORRAG_ENV=dev`. Unauthenticated
development is an explicit opt-in: uncomment `HARBORRAG_AUTH_MODE=none` and
`HARBORRAG_ALLOW_INSECURE_DEV=true` in `env/.env.api`. The container's internal
wildcard listener needs that acknowledgement, which is safe only while the
published address remains loopback, and `dev.sh` prints a warning on every start.
Even with that acknowledgement, the API answers `421` to any request whose Host header is not `localhost`, `127.0.0.1` or `[::1]`, so use those names in the browser.

Upgrading an existing checkout: an `env/.env.api` copied from an older template
still has `HARBORRAG_AUTH_MODE=none`, and `bootstrap` only adds the missing
secret. Set `HARBORRAG_AUTH_MODE=hmac` in it, then rebuild the API image with
`dev.sh api --build` so `dev.sh token` is available. `dev.sh api` refuses `none`
without `HARBORRAG_ALLOW_INSECURE_DEV=true` or on a non-loopback bind address.

Authentication and CORS values belong in the ignored
`env/.env.api`. The API receives chat and embedding credentials from
`env/.env.models` for chat completions and synchronous vector retrieval, while
connector credentials remain isolated in ingestion workers. Use
`dev.sh up --no-worker` to omit the worker. Stop the
development topology in reverse order with:

```bash
scripts/deployment/dev.sh down
```

Individual ownership is explicit: `temporal` never starts a worker, `worker`
only starts the worker, and `api` starts the API with `--no-deps`.

`deploy/compose/docker-compose.yml` is the single API composition. It joins the
external data network created by `docker-compose.database.yml` and connects to
the Temporal service created by `docker-compose.temporal.yml`. Environment
policy, rather than duplicated development and production Compose files,
controls the API mode. Production deployments must supply authentication and
their own secret/TLS/network policy.

Every `HARBORRAG_*_ALLOW_INSECURE_REMOTE` acknowledgement in the API and worker
Compose files defaults to `true`, because the defaults address plaintext Compose
service names (`temporal`, `redis`, `minio`, `qdrant`, `falkordb`) on the
private `harborrag-data-network`. Each one is an ordinary interpolation. When
you point a URL or host at a TLS endpoint, set the matching variable to `false`
in the environment file passed to Compose, and the adapter then refuses
plaintext instead of silently accepting it. The runtime honours every one of
these acknowledgements only when `HARBORRAG_ENV=dev`; with `HARBORRAG_ENV=prod`
plaintext non-loopback endpoints are refused regardless of the flag.

The API, worker and MCP server never receive the MinIO root credentials. The
`minio-init` one-shot in `docker-compose.database.yml` creates the
`harborrag-raw` and `harborrag-artifacts` buckets and an application user
(`MINIO_APP_ACCESS_KEY`, default `harborrag-app`, with the generated
`MINIO_APP_SECRET_KEY`) whose policy, `deploy/minio/harborrag-app-policy.json`,
allows only listing those two buckets and reading, writing and deleting their
objects (plus listing bucket names, which the health check uses). `dev.sh data`
(and `up`) runs it after the data services start; it is safe to rerun. Because
the application key cannot create buckets, a store whose buckets are missing
fails at startup instead of creating them. After upgrading a checkout whose data
stack is already running, run `dev.sh data` once before `dev.sh worker` or
`dev.sh api`: until `minio-init` has run, the application user does not exist
and every object-store call is refused.

## CLI and MCP images

Build the standalone operator images from the repository root:

```bash
docker build -f deploy/docker/Dockerfile.cli -t harborrag-cli .
docker build -f deploy/docker/Dockerfile.mcp -t harborrag-mcp .

docker run --rm harborrag-cli --help
docker run --rm harborrag-mcp --check
```

Both images include the shared runtime, model and retrieval dependencies,
checked-in configuration, and packaged chat prompts. They run as the non-root
`harborrag` user. Configuration paths inside the images point to `/app/config`;
mount a replacement directory there read-only when deploying a different
catalog.

The CLI image uses `harborrag` as its entry point. Provide
`env/.env.models` for chat and the relevant service settings for retrieval or
ingestion commands:

```bash
docker run --rm \
  --env-file env/.env.models \
  harborrag-cli chat "Explain HarborRAG." --json
```

The MCP image uses `python -m harborrag_mcp_server` as its entry point and
defaults to stdio. An MCP client must keep stdin attached with `-i`; real tool
calls also require protected model/database settings and network-reachable data
services. Its writable JSONL audit path is
`/var/lib/harborrag/.harborrag/mcp-audit.jsonl`.

Use `scripts/deployment/mcp.sh --http` for the authenticated loopback HTTP
transport and local status/configuration UI. Remote MCP needs TLS and a
production JWT/JWKS verifier and is intentionally not provided by the local
container command.

The Explorer MCP UI server is optional and separate. `scripts/deployment/mcp-ui.sh`
builds `deploy/docker/Dockerfile.mcp-ui`, a layer over the MCP image that adds
the `ui` extra and runs `harborrag-mcp-ui`, and starts it from
`deploy/compose/docker-compose.mcp-ui.yml` on port `8011`. Its audit path is the
same, inside its own `harborrag-mcp-ui-audit` volume. The MCP image and
`mcp.sh` never include or start it.

## Model assets

`scripts/models/` contains helpers for Docling/FastEmbed downloads, warmup, and local-model smoke checks. Inspect each script before use: provider/model downloads may require network access, substantial disk space, and platform-specific runtimes. Keep caches outside container layers when they need independent lifecycle management.

## Temporal and cloud directories

`deploy/temporal/` contains PostgreSQL schema setup, dynamic configuration, and
namespace initialization for local development. It is not a production
topology; see its README for the Temporal Cloud/Helm boundary. `deploy/aws/`
reserves cloud deployment directions and does not provide complete
infrastructure-as-code.

## Production readiness checklist

Before an internet-facing deployment can be considered production-ready,
operators still need deployment-specific authorization policy, TLS/network
policy, secret delivery, backup/restore, resource limits, alert thresholds, and
end-to-end tests against their chosen source systems. Review the
[runtime reliability boundary](../architecture/runtime-reliability.md) before
choosing worker, retry, timeout, retention, and recovery policy.
