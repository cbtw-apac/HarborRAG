# Deployment

Deployment assets are at mixed maturity. The database Compose file is suitable for local adapter smoke tests. Application, MCP, and Temporal images are design scaffolding and reference package extras or entry points that do not exist yet.

## Local repository services

`deploy/compose/docker-compose.database.yml` defines:

- Qdrant on ports 6333/6334;
- FalkorDB on ports 6379/3000;
- Redis on host port 6380 by default;
- PostgreSQL on port 5432.

Prepare a protected environment file:

```bash
cp env-example/.env.database.example env/.env.database
export DATABASE_ENV_FILE=env/.env.database
scripts/deployment/database_up.sh
```

The script requires `DATABASE_ENV_FILE` and passes it to Docker Compose. It does not create the file automatically. Change the example password before using the stack outside an isolated developer machine.

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

The following files describe intended topology but are not runnable application deployments at the current revision:

| File | Current blocker |
| --- | --- |
| `docker-compose.yml` | Refers to qdrant/redis services not defined in the file and builds unfinished app images |
| `docker-compose.dev.yml` | Builds the unfinished API image and expects root `.env` |
| `docker-compose.prod.yml` | Depends on qdrant/redis services not defined in the file and builds the unfinished API image |
| `docker-compose.temporal.yml` | Builds a worker around placeholder Temporal modules |
| `docker-compose.all.yml` | Includes the incomplete dev and Temporal stacks |

Current Dockerfile gaps include a missing FastAPI factory, undeclared `api`/`mcp`/`temporal` extras, a missing `harbor` console script, and a missing MCP server factory. Resolve and test those package entry points before presenting the images as deployable.

## Model assets

`scripts/models/` contains helpers for Docling/FastEmbed downloads, warmup, and local-model smoke checks. Inspect each script before use: provider/model downloads may require network access, substantial disk space, and platform-specific runtimes. Keep caches outside container layers when they need independent lifecycle management.

## Temporal and cloud directories

`deploy/temporal/` contains local dynamic configuration and namespace metadata, but runtime workflow/activity/client modules remain placeholders. `deploy/aws/` reserves cloud deployment directions and does not provide complete infrastructure-as-code.

## Production readiness checklist

Before an application deployment can be considered production-ready, the repository still needs implemented service entry points, unified configuration/composition, authentication and authorization, tenant context propagation, migrations, health/readiness semantics, secret management, TLS/network policy, backup/restore, observability wiring, resource limits, and end-to-end tests against pinned service versions.
