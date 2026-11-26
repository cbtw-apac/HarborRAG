# Repository smoke checks

These standalone scripts run real storage operations through HarborRAG's repository APIs against
Qdrant and FalkorDB. A pass shows that each backend accepts the connection and stores, reads and
removes a probe record. They are opt-in, and pytest never collects them.

Every target must be disposable. Read the shared
[safety rules and exit codes](../../README.md#real-system-smoke-tests) before you point a script
at a service that is not on your machine.

## Before you start

- Run every command from the repository root.
- The scripts need the `qdrant-client` and `falkordb` client libraries. The checkout
  environment in
  [Running HarborRAG from a checkout](../../../../../docs/developers/checkout-quick-start.md)
  installs them with the `production` extra. A script whose client is missing exits `2` and
  prints the install command; to add both at once:

  ```bash
  uv pip install -e "packages/harborrag-adapters[qdrant,falkordb]"
  ```

- Start the local data services. `dev.sh bootstrap` creates `env/.env.database` from its template
  if it does not exist; review its ports before `dev.sh data` starts the services:

  ```bash
  scripts/deployment/dev.sh bootstrap
  scripts/deployment/dev.sh data
  ```

## Run the checks

Load the same database settings and run both checks:

```bash
HARBOR_SMOKE_ENV_FILE=env/.env.database \
  uv run python packages/harborrag-adapters/tests/repositories/smoke/run_all.py
```

To check one backend, replace `run_all.py` with `qdrant.py` or `falkordb_graph.py`. Each script
prints one line when it passes, in this shape:

```text
Qdrant smoke passed: searched and deleted point='point-<suffix>' in 'repository_probe'
```

`run_all.py` exits `0` only when both pass, `1` when any operation fails, including a service
that is not running, and `2` when a client library is missing.

## What each check does

The default endpoints match the ports in `deploy/compose/docker-compose.database.yml`.

| Backend | Default endpoint | Operations |
| --- | --- | --- |
| Qdrant | `http://127.0.0.1:6333` | Creates a probe collection, upserts, retrieves and searches a point, then deletes the point and the collection |
| FalkorDB | `127.0.0.1:6379`, graph `harborrag_smoke` | Upserts two nodes and an edge, expands them, then deletes the nodes |

## Change the endpoints

| Variable | What it changes |
| --- | --- |
| `HARBOR_SMOKE_QDRANT_URL` | The full Qdrant URL; otherwise `QDRANT_HTTP_PORT` on `127.0.0.1` |
| `HARBOR_SMOKE_QDRANT_COLLECTION`, `HARBOR_SMOKE_QDRANT_PREFIX`, `HARBOR_SMOKE_QDRANT_PREFER_GRPC` | The probe collection name (`repository_probe`), its namespace prefix (`harborrag_smoke`) and gRPC use (`false`) |
| `FALKORDB_HOST`, `FALKORDB_PORT`, `FALKORDB_USERNAME`, `FALKORDB_PASSWORD`, `FALKORDB_SSL` | The FalkorDB connection |
| `HARBOR_SMOKE_FALKORDB_GRAPH` | The probe graph (`harborrag_smoke`) |
