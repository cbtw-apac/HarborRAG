# Repository smoke checks

These standalone checks exercise real storage operations through HarborRAG's
repository APIs. They are opt-in and are not collected by pytest.

Read the shared [smoke-test safety and exit-code guidance](../README.md) before
using any non-local service. All targets should be disposable.

Install the provider clients before running the container-backed checks:

```bash
uv pip install -e \
  "packages/harborrag-adapters[redis,qdrant,falkordb,postgres]"
```

Create the local database environment from the tracked template, review its
credentials and ports, then start the Compose stack:

```bash
cp env-example/.env.database.example env/.env.database
export DATABASE_ENV_FILE=env/.env.database
scripts/deployment/database_up.sh
```

With the local database Compose stack running, load the same environment and
run all five checks:

```bash
HARBOR_SMOKE_ENV_FILE=env/.env.database \
  .venv/bin/python packages/harborrag-adapters/tests/smoke/repositories/run_all.py
```

Run a single backend by replacing `run_all.py` with `sqlite.py`,
`postgresql.py`, `redis_cache.py`, `qdrant.py`, or `falkordb_graph.py`.

`run_all.py` returns `1` when a configured operation fails, `2` when any target
is unavailable, and `0` only when all five targets pass. Run `sqlite.py`
independently when the container-backed services are intentionally absent.

The default endpoints match `deploy/compose/docker-compose.database.yml`:

| Backend | Default endpoint | Operation |
| --- | --- | --- |
| SQLite | Temporary local file | Commit and reload document/chunk/outbox data |
| PostgreSQL | `127.0.0.1:5432` | Create schema, then write/read in a rolled-back transaction |
| Redis | `redis://127.0.0.1:6380/15` | Set, compare-and-set, read, and delete |
| Qdrant | `http://127.0.0.1:6333` | Ensure collection, upsert, retrieve, search, and delete point |
| FalkorDB | `127.0.0.1:6379` | Upsert nodes/edge, expand, and delete nodes |

Override endpoints with `HARBOR_SMOKE_POSTGRES_URL`,
`HARBOR_SMOKE_REDIS_URL`, `HARBOR_SMOKE_QDRANT_URL`, `FALKORDB_HOST`, and
`FALKORDB_PORT`. The standard `POSTGRES_*`, `REDIS_PORT`, and Qdrant port
variables from `env/.env.database` are also recognized.

Only use a disposable database. PostgreSQL creates the HarborRAG tables when
they are absent, and FalkorDB uses a separate `harborrag_smoke` graph by
default. Qdrant collections and other probe records are deleted or rolled back.
