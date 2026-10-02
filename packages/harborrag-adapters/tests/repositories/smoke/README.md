# Repository smoke checks

These standalone checks exercise real storage operations through HarborRAG's
repository APIs. They are opt-in and are not collected by pytest.

Read the shared [smoke-test safety and exit-code
guidance](../../README.md#real-system-smoke-tests) before using any non-local
service. All targets should be disposable.

Install the provider clients before running the container-backed checks:

```bash
uv pip install -e \
  "packages/harborrag-adapters[qdrant,falkordb]"
```

Create the local database environment from the tracked template, review its
credentials and ports, then start the Compose stack:

```bash
cp env-example/.env.database.example env/.env.database
scripts/deployment/dev.sh data
```

With the local database Compose stack running, load the same environment and
run both checks:

```bash
HARBOR_SMOKE_ENV_FILE=env/.env.database \
  .venv/bin/python packages/harborrag-adapters/tests/repositories/smoke/run_all.py
```

Run a single backend by replacing `run_all.py` with `qdrant.py` or
`falkordb_graph.py`.

`run_all.py` returns `1` when a configured operation fails, `2` when any target
is unavailable, and `0` only when both targets pass.

The default endpoints match `deploy/compose/docker-compose.database.yml`:

| Backend | Default endpoint | Operation |
| --- | --- | --- |
| Qdrant | `http://127.0.0.1:6333` | Ensure collection, upsert, retrieve, search, and delete point |
| FalkorDB | `127.0.0.1:6379` | Upsert nodes/edge, expand, and delete nodes |

Override endpoints with `HARBOR_SMOKE_QDRANT_URL`, `FALKORDB_HOST`, and
`FALKORDB_PORT`. The standard Qdrant port variables from `env/.env.database`
are also recognized.

Only use a disposable database. FalkorDB uses a separate `harborrag_smoke` graph
by default. Qdrant collections and other probe records are deleted or rolled back.
