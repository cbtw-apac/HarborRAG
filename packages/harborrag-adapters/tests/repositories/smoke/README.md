# Repository smoke checks

These standalone scripts run real storage operations through HarborRAG's repository APIs against
SQLite, PostgreSQL, Redis, Qdrant and FalkorDB. A pass shows that each backend accepts the
connection and stores, reads and removes a probe record. They are opt-in, and pytest never
collects them.

Every target must be disposable. Read the shared
[safety rules and exit codes](../../README.md#real-system-smoke-tests) before you point a script
at a service that is not on your machine.

## Before you start

- Run every command from the repository root.
- The scripts need the `redis`, `qdrant-client`, `falkordb` and `asyncpg` client libraries. The
  checkout environment in
  [Running HarborRAG from a checkout](../../../../../docs/developers/checkout-quick-start.md)
  installs them with the `production` extra. A script whose client is missing exits `2` and prints the install
  command; to add all four at once:

  ```bash
  uv pip install -e "packages/harborrag-adapters[redis,qdrant,falkordb,postgres]"
  ```

- Start the local data services. `dev.sh bootstrap` creates `env/.env.database` from its template
  if it does not exist; review its credentials and ports before `dev.sh data` starts the services:

  ```bash
  scripts/deployment/dev.sh bootstrap
  scripts/deployment/dev.sh data
  ```

## Run the checks

Load the same database settings and run all five checks:

```bash
HARBOR_SMOKE_ENV_FILE=env/.env.database \
  uv run python packages/harborrag-adapters/tests/repositories/smoke/run_all.py
```

To check one backend, replace `run_all.py` with `sqlite.py`, `postgresql.py`, `redis_cache.py`,
`qdrant.py` or `falkordb_graph.py`. `sqlite.py` needs no service, so run it alone when the
containers are not running. Each script prints one line when it passes, for example:

```text
SQLite smoke passed: committed document_id='document-3960a084a5eb' in a temporary database
```

`run_all.py` exits `0` only when all five pass, `1` when any operation fails, including a service
that is not running, and `2` when a client library is missing.

## What each check does

The default endpoints match the ports in `deploy/compose/docker-compose.database.yml`.

| Backend | Default endpoint | Operations |
| --- | --- | --- |
| SQLite | A temporary local file | Commits a document, chunk and outbox record, then reloads them |
| PostgreSQL | `127.0.0.1:5432`, database `harborrag` | Creates the HarborRAG tables if absent, then writes and reads in a transaction it rolls back |
| Redis | `redis://127.0.0.1:6380/15` | Sets, compare-and-sets, reads and deletes a key |
| Qdrant | `http://127.0.0.1:6333` | Creates a probe collection, upserts, retrieves and searches a point, then deletes the point and the collection |
| FalkorDB | `127.0.0.1:6379`, graph `harborrag_smoke` | Upserts two nodes and an edge, expands them, then deletes the nodes |

PostgreSQL connects to the database `POSTGRES_DB` names. With `env/.env.database` that is the
development stack's own database, so the tables are created there when missing; point
`HARBOR_SMOKE_POSTGRES_URL` at another database to keep the stack untouched.

## Change the endpoints

| Variable | What it changes |
| --- | --- |
| `HARBOR_SMOKE_POSTGRES_URL` | The full PostgreSQL URL; otherwise built from `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_HOST`, `POSTGRES_PORT` and `POSTGRES_DB` |
| `HARBOR_SMOKE_REDIS_URL` | The full Redis URL; otherwise `REDIS_PORT` on `127.0.0.1`, database `15` |
| `HARBOR_SMOKE_QDRANT_URL` | The full Qdrant URL; otherwise `QDRANT_HTTP_PORT` on `127.0.0.1` |
| `HARBOR_SMOKE_QDRANT_COLLECTION`, `HARBOR_SMOKE_QDRANT_PREFIX`, `HARBOR_SMOKE_QDRANT_PREFER_GRPC` | The probe collection name (`repository_probe`), its namespace prefix (`harborrag_smoke`) and gRPC use (`false`) |
| `FALKORDB_HOST`, `FALKORDB_PORT`, `FALKORDB_USERNAME`, `FALKORDB_PASSWORD`, `FALKORDB_SSL` | The FalkorDB connection |
| `HARBOR_SMOKE_FALKORDB_GRAPH` | The probe graph (`harborrag_smoke`) |
