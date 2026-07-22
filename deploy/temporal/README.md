# HarborRAG Temporal deployment

The local stack uses PostgreSQL for both Temporal persistence stores:

- `temporal` stores workflow history, mutable state, and tasks;
- `temporal_visibility` stores workflow visibility and search data.

It follows Temporal's current
[PostgreSQL Compose sample](https://github.com/temporalio/samples-server/blob/main/compose/docker-compose-postgres.yml)
and pins the image versions directly in
`deploy/compose/docker-compose.temporal.yml`. The environment file contains
runtime settings and credentials only. This stack is for development and
integration testing. For production, use Temporal Cloud
or the official [Temporal Helm chart](https://github.com/temporalio/helm-charts)
with externally managed PostgreSQL, credentials, backups, and schema upgrades.

## Start the local server

```bash
scripts/deployment/temporal_up.sh
```

The first run creates `env/.env.temporal`. Review the local password and public
Temporal/UI ports there. PostgreSQL is reachable only by containers on the
Temporal network and does not consume a host port. The startup sequence waits
for PostgreSQL, initializes both SQL
schemas, starts Temporal, creates the `harborrag` namespace, and then starts the
UI at <http://localhost:8080>.

The PostgreSQL data is retained in the named
`harborrag-temporal-postgresql-data` volume. A regular Compose stop or
down does not delete it. The startup script creates this external volume when
it is missing. It also updates the persisted PostgreSQL role password from
`TEMPORAL_POSTGRES_PASSWORD` before running schema migrations, so changing the
local development credential does not require deleting the volume.

## Start HarborRAG workers

Start Qdrant and FalkorDB first:

```bash
DATABASE_ENV_FILE=env/.env.database scripts/deployment/database_up.sh
```

The database stack creates the stable `harborrag-data-network`. The Temporal
worker joins that network and the Compose-managed Temporal default network, so
it can reach Qdrant, FalkorDB, and Redis by service name. The Temporal server,
UI, schema initializer, and PostgreSQL remain isolated from the data services.
Starting the worker profile before the database stack fails because its data
network is intentionally external to the Temporal Compose project.

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
HARBORRAG_CONNECTOR_CONFIG_PATH=config/connectors.yaml
HARBORRAG_PARSER_CONFIG_PATH=config/parsers.yaml
HARBORRAG_MODEL_CONFIG_PATH=config/models.yaml
HARBORRAG_LOCAL_SOURCE_MOUNT=../../docs
```

Run `scripts/deployment/temporal_up.sh` again. Worker checkpoints and ingestion
objects persist in the `harborrag-ingestion-data` volume. A custom
`HARBORRAG_TEMPORAL_DEPENDENCY_PROVIDER` is optional. Two worker replicas are
the local default; lower `HARBORRAG_TEMPORAL_WORKER_REPLICAS` to `1` on a
memory-constrained host. The replicas share the persistent
`harborrag-model-cache` volume, so model weights downloaded by one worker are
reused by the others and survive container replacement. The startup script
also creates this external volume when the worker profile is enabled.

Submit a run from the host:

```bash
HARBORRAG_TEMPORAL_TARGET=localhost:7233 \
  uv run --package harborrag-app harbor ingest start \
  --tenant tenant-1 \
  --connector local-docs \
  --wait
```

## Verify persistence

```bash
docker compose \
  --env-file env/.env.temporal \
  --file deploy/compose/docker-compose.temporal.yml \
  exec postgresql psql -U temporal -l
```

Both `temporal` and `temporal_visibility` should be present. Schema setup and
namespace creation run in the one-shot `temporal-schema` and
`temporal-namespace` services.

Removing the named volume permanently deletes local workflow history, so use
`docker compose down --volumes` only when an intentional clean reset is needed.
