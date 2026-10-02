# Repository smoke checks

These standalone checks exercise real storage operations through HarborRAG's
repository APIs. They are opt-in and are not collected by pytest.

Read the shared [smoke-test safety and exit-code
guidance](../../README.md#real-system-smoke-tests) before using any non-local
service. All targets should be disposable.

Install the provider clients before running the container-backed checks:

```bash
uv pip install -e \
  "packages/harborrag-adapters[qdrant]"
```

Create the local database environment from the tracked template, review its
credentials and ports, then start the Compose stack:

```bash
cp env-example/.env.database.example env/.env.database
scripts/deployment/dev.sh data
```

With the local database Compose stack running, load the same environment and
run the check:

```bash
HARBOR_SMOKE_ENV_FILE=env/.env.database \
  .venv/bin/python packages/harborrag-adapters/tests/repositories/smoke/run_all.py
```

Run the backend directly by replacing `run_all.py` with `qdrant.py`.

`run_all.py` returns `1` when a configured operation fails, `2` when any target
is unavailable, and `0` only when every target passes.

The default endpoints match `deploy/compose/docker-compose.database.yml`:

| Backend | Default endpoint | Operation |
| --- | --- | --- |
| Qdrant | `http://127.0.0.1:6333` | Ensure collection, upsert, retrieve, search, and delete point |

Override the endpoint with `HARBOR_SMOKE_QDRANT_URL`. The standard Qdrant port
variables from `env/.env.database` are also recognized.

Only use a disposable database. Qdrant collections and other probe records are deleted or rolled back.
