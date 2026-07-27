# HarborRAG Temporal deployment

The local stack reuses the PostgreSQL service from
`deploy/compose/docker-compose.database.yml` for both Temporal persistence
stores:

- `temporal` stores workflow history, mutable state, and tasks;
- `temporal_visibility` stores workflow visibility and search data.

It follows Temporal's current
[PostgreSQL Compose sample](https://github.com/temporalio/samples-server/blob/main/compose/docker-compose-postgres.yml)
and pins the image versions directly in
`deploy/compose/docker-compose.temporal.yml`. It does not start a second
PostgreSQL container. The environment file contains runtime settings and
credentials only. This stack is for development and
integration testing. For production, use Temporal Cloud
or the official [Temporal Helm chart](https://github.com/temporalio/helm-charts)
with externally managed PostgreSQL, credentials, backups, and schema upgrades.

## Start the local server

```bash
DATABASE_ENV_FILE=env/.env.database scripts/deployment/database_up.sh
scripts/deployment/temporal_up.sh
```

The first run creates `env/.env.temporal` with private file permissions.
`temporal_up.sh` reads `POSTGRES_USER` and `POSTGRES_PASSWORD` from
`DATABASE_ENV_FILE` (`env/.env.database` by default), so the Temporal server and
schema job use the credentials of the already-deployed database stack rather
than maintaining a second copy. Keep the worker's percent-encoded
`HARBORRAG_INGESTION_STATE_URL` aligned with those values, then review the
public Temporal/UI ports. PostgreSQL is reached over the existing
`harborrag-data-network`. The startup sequence waits for it, initializes both SQL
schemas, starts Temporal, creates the `harborrag` namespace, and then starts the
UI at <http://localhost:8080>.

Workflow history is retained by the database stack's `pg_data` volume. A
regular Temporal Compose stop or down does not affect it. Schema initialization
is idempotent; the configured PostgreSQL role must be allowed to create and
migrate the `temporal` and `temporal_visibility` databases.

## Start HarborRAG workers

Start the complete database stack first:

```bash
DATABASE_ENV_FILE=env/.env.database scripts/deployment/database_up.sh
```

The database stack creates the stable `harborrag-data-network`. Temporal's
schema initializer and server use that network to reach the existing
`postgres` service. The worker also joins it and reaches PostgreSQL, Qdrant,
FalkorDB, and Redis by service name. Startup fails early when the external
network is absent instead of silently launching another database.

The active runtime configuration is checked in at `config/connectors.yaml`,
`config/parsers.yaml`, and `config/models.yaml`. Keep credentials in the ignored
files under `env/`; the worker loads those files without exposing Temporal's
PostgreSQL password. Create an environment file from its template only when it
does not already exist:

```bash
test -f env/.env.connector || cp env-example/.env.connector.example env/.env.connector
test -f env/.env.parser || cp env-example/.env.parser.example env/.env.parser
test -f env/.env.models || cp env-example/.env.models.example env/.env.models
```

`config/models.yaml` resolves its provider names, model names, and API keys from
`HARBOR_CHAT_*` and `HARBOR_EMBED_*` in `env/.env.models`; secret
values never belong in the YAML file.

Point `env/.env.temporal` at those files and enable the built-in worker graph:

```dotenv
TEMPORAL_START_WORKER=1
HARBORRAG_TEMPORAL_WORKER_REPLICAS=2
HARBORRAG_CONNECTOR_CONFIG_PATH=/app/config/connectors.yaml
HARBORRAG_PARSER_CONFIG_PATH=/app/config/parsers.yaml
HARBORRAG_MODEL_CONFIG_PATH=/app/config/models.yaml
```

Those three are paths *inside* the worker container. The image copies the
tracked `config/` directory to `/app/config` and runs from its writable data
directory, so repository-relative values do not resolve. The image sets the same
absolute defaults; override them only to point at a mounted configuration
directory. `--build` runs on every startup, so edits to the tracked YAML files
are picked up on the next `scripts/deployment/temporal_up.sh`.

The local connector's source directory is not repeated here. It is configured
once, as `LOCAL_SOURCE_PATH` in `env/.env.connector`, which
`config/connectors.yaml` maps to the local connector's `source_path`.
`scripts/deployment/temporal_up.sh` resolves that value against the repository
root and mounts the directory read-only at `/data/sources` inside the worker,
where the container's own `LOCAL_SOURCE_PATH` points. It defaults to `docs` and
fails if the directory is missing.

Run `scripts/deployment/temporal_up.sh` again. Worker checkpoints and operational
state use PostgreSQL; ingestion objects remain in the
`harborrag-ingestion-data` volume. A custom
`HARBORRAG_TEMPORAL_DEPENDENCY_PROVIDER` is optional. Two worker replicas are
the local default; lower `HARBORRAG_TEMPORAL_WORKER_REPLICAS` to `1` on a
memory-constrained host. The replicas share the persistent
`harborrag-model-cache` volume, so model weights downloaded by one worker are
reused by the others and survive container replacement. The startup script
also creates this external volume when the worker profile is enabled.

Submit a run from the host:

```bash
HARBORRAG_TEMPORAL_TARGET=localhost:7233 \
  uv run --package harborrag-app harborrag ingest start \
  --tenant tenant-1 \
  --connector local-docs \
  --wait
```

## Verify persistence

```bash
docker compose \
  --env-file env/.env.database \
  --file deploy/compose/docker-compose.database.yml \
  exec postgres psql -U postgres -l
```

Both `temporal` and `temporal_visibility` should be present. Schema setup and
namespace creation run in the one-shot `temporal-schema` and
`temporal-namespace` services.

Removing the database stack's `pg_data` volume permanently deletes Temporal
history and HarborRAG PostgreSQL state, so remove it only for an intentional
clean reset.
