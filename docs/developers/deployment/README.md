# Deployment

Deployment assets are at mixed maturity. The database Compose file supports
local adapter smoke tests, the Temporal Compose file provides a runnable
PostgreSQL-backed development server, and the development helper composes those
services with the control-plane API. Production and MCP deployments still
require application-specific composition.

## Local repository services

`deploy/compose/docker-compose.database.yml` defines:

- Qdrant on ports 6333/6334;
- FalkorDB on ports 6379/3000;
- Redis on host port 6380 by default;
- PostgreSQL on port 5432.

The service image tags are pinned directly in the Compose file. The environment
template only controls ports, credentials, and startup behavior.

Prepare a protected environment file:

```bash
cp env-example/.env.database.example env/.env.database
export DATABASE_ENV_FILE=env/.env.database
scripts/deployment/database_up.sh
```

The script requires `DATABASE_ENV_FILE` and passes it to Docker Compose. It does not create the file automatically. Change the example password before using the stack outside an isolated developer machine.

The stack owns the stable `harborrag-data-network`. Application containers and
the Temporal worker may join this network, but Temporal control-plane services
remain on their separate `harborrag-temporal-network`. Start the database stack
before enabling the Temporal worker profile.

Use the repository smoke runner in [Testing](../testing/README.md#real-system-smoke-checks) to verify adapters through their public APIs.

## Monitoring stack

`docker-compose.monitoring.yml` defines Prometheus, Grafana, and Loki using files under `deploy/prometheus`, `deploy/grafana`, and `deploy/loki`. It is infrastructure scaffolding: the current application does not expose a production metrics/logging integration wired to this stack.

The helper script expects a root `.env` or tries to copy a missing `.env.example`, so create `.env` explicitly before using it:

```bash
touch .env
scripts/deployment/monitoring_up.sh
```

Set non-default Grafana credentials in that environment for any non-local use.

## Application Compose files

`scripts/deployment/dev_up.sh` is the supported local entrypoint for the full
development topology. On its first invocation it creates protected environment
files from `env-example/`, then stops so placeholder credentials can be
reviewed. Run it again to start the data services, Temporal, a Temporal worker,
and the API:

```bash
scripts/deployment/dev_up.sh
```

The API binds to `127.0.0.1:8000` by default. Override
`HARBORRAG_API_BIND_ADDRESS` or `HARBORRAG_API_PORT` when another local binding
is required. Set `HARBORRAG_DEV_START_WORKER=0` to omit the ingestion worker.
Stop the API and its development dependencies in reverse order with:

```bash
scripts/deployment/dev_down.sh
```

The remaining application Compose files describe intended topology but are not
complete production deployments:

| File | Current boundary |
| --- | --- |
| `docker-compose.yml` | Legacy API/CLI composition; external data services are not defined in the file |
| `docker-compose.dev.yml` | API portion of the supported `dev_up.sh` topology |
| `docker-compose.prod.yml` | Production outline; external data services and deployment-specific secrets remain operator concerns |
| `docker-compose.temporal.yml` | Runnable local Temporal server and built-in worker profile |
| `docker-compose.all.yml` | Legacy include composition; use `dev_up.sh` for the validated development topology |

## Model assets

`scripts/models/` contains helpers for Docling/FastEmbed downloads, warmup, and local-model smoke checks. Inspect each script before use: provider/model downloads may require network access, substantial disk space, and platform-specific runtimes. Keep caches outside container layers when they need independent lifecycle management.

## Temporal and cloud directories

`deploy/temporal/` contains PostgreSQL schema setup, dynamic configuration, and
namespace initialization for local development. It is not a production
topology; see its README for the Temporal Cloud/Helm boundary. `deploy/aws/`
reserves cloud deployment directions and does not provide complete
infrastructure-as-code.

## Production readiness checklist

Before an application deployment can be considered production-ready, the repository still needs implemented service entry points, unified configuration/composition, authentication and authorization, tenant context propagation, migrations, health/readiness semantics, secret management, TLS/network policy, backup/restore, observability wiring, resource limits, and end-to-end tests against pinned service versions.
