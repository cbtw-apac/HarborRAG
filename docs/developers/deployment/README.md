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
- Redis on host port 6380 by default;
- PostgreSQL on port 5432.

The service image tags are pinned directly in the Compose file. The environment
template only controls ports, credentials, and startup behavior.

FalkorDB persists RDB snapshots in its named volume. Do not enable AOF for this
service: FalkorDB 4.20.1 can create and restore the knowledge-graph uniqueness
constraint from a snapshot, but crashes when Redis replays the asynchronous
`GRAPH.CONSTRAINT` command from AOF. The separate Redis service continues to use
AOF persistence.

Prepare a protected environment file:

```bash
cp env-example/.env.database.example env/.env.database
scripts/deployment/dev.sh data
```

The unified deployment helper accepts `DATABASE_ENV_FILE` overrides and passes
the selected file to Docker Compose. Change the example password before using
the stack outside an isolated developer machine.

The stack owns the stable `harborrag-data-network`. Application containers and
the Temporal worker may join this network, but Temporal control-plane services
remain on their separate `harborrag-temporal-network`. Start the database stack
before enabling the Temporal worker profile.

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

```bash
cp env-example/.env.monitoring.example env/.env.monitoring
chmod 600 env/.env.monitoring
# Set GRAFANA_ADMIN_PASSWORD in env/.env.monitoring, then run:
docker compose --env-file env/.env.monitoring \
  --file deploy/compose/docker-compose.monitoring.yml up --detach
```

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
is required. The local container's internal wildcard listener is explicitly
acknowledged by `HARBORRAG_ALLOW_INSECURE_DEV=true`; this is safe only while the
published address remains loopback. Configure HMAC authentication before using
a non-loopback published address. Authentication and CORS values belong in the ignored
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
end-to-end tests against their chosen source systems.
