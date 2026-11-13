# HarborRAG CLI

Find where each `harborrag` command lives, how the CLI loads its environment, and the
failure modes worth knowing before you change this package. The `harborrag_app.cli`
package is the operator-facing command line. It renders workflow, retrieval and chat
results, and delegates every operation to the same transport-neutral application service
as the Control Plane API.

These notes are for contributors. What each command does for a user is in the
[CLI reference](../../../../../docs/users/cli-reference/README.md), and
`uv run harborrag COMMAND --help` lists every option.

## Install and run from a checkout

The CLI runs from inside a HarborRAG repository checkout. Install the production
dependencies that ingestion and hybrid retrieval need:

```bash
uv sync --package harborrag-app --extra production
uv run harborrag --help
```

The base package is enough for help and presentation code. Live commands also need their
services:

- `doctor` checks the project, the catalogs, the environment and the local services, and
  Temporal only with `--temporal`.
- `ingest start` and the observe and control actions need Temporal; `ingest run` runs the
  ingestion in the current process, without Temporal or a worker.
- `chat` needs the configured chat model and its credentials.
- `retrieve` needs the configured embedding model, Qdrant, FalkorDB and the ingestion
  object store.
- Writes from a checkout need the stack's Postgres as the control database; see
  [Point a checkout at the stack's control database](#point-a-checkout-at-the-stacks-control-database).

Start the repository's development stack with:

```bash
scripts/deployment/dev.sh up
```

The non-root CLI image, `deploy/docker/Dockerfile.cli`, is built and run as described in
[Build the CLI and MCP images](../../../../../docs/developers/deployment/README.md#build-the-cli-and-mcp-images).

## Find a command

The eight top-level commands, in the order `harborrag --help` groups them, and the
`ingest` actions:

| Command | What it does | Code in this folder |
| --- | --- | --- |
| `harborrag doctor` | Check project, catalogs, environment and local services; Temporal only with `--temporal` | `commands/doctor.py`, `doctor/` |
| `harborrag auth` | Create, list and revoke MCP reader keys (`auth keys create`, `list`, `revoke`) | `commands/auth.py`, `commands/auth_keys.py` |
| `harborrag config` | Show or apply a settings file as one revision (`import`); export stored overrides (`export`) | `commands/config.py`, `config_views.py` |
| `harborrag chat` | Answer one message, grounded in retrieval, with session memory | `commands/chat.py` |
| `harborrag retrieve` | Search the indexed corpus | `commands/retrieve.py` |
| `harborrag ingest run` | Run an ingestion in the current process, without Temporal | `commands/ingest_run.py` |
| `harborrag ingest start` | Submit a durable ingestion run | `commands/ingest.py` |
| `harborrag ingest status` | Read current progress and attention queues | `commands/ingest.py` |
| `harborrag ingest wait` | Wait for the final result | `commands/ingest.py` |
| `harborrag ingest watch` | Follow a run with the inline Rich progress block | `commands/ingest.py` |
| `harborrag ingest pause` | Request a durable pause | `commands/ingest.py` |
| `harborrag ingest resume` | Resume a paused run | `commands/ingest.py` |
| `harborrag ingest cancel` | Cancel at a safe workflow boundary | `commands/ingest.py` |
| `harborrag topology` | Configure and operate semantic topology enrichment | `commands/topology.py`, `commands/topology_security.py` |
| `harborrag source` | Manage stored connections (`list`, `show`, `add`, `update`, `test`, `sync`, `remove`) | `commands/source.py`, `source_support.py`, `source_views.py` |

`main.py` registers the commands and resolves the checkout or project directory before
any command builds a service; `project.py` holds that lookup.

## Load the environment files

What the CLI reads from `env/` is in
[Run from a repository checkout](../../../../../docs/users/cli-reference/README.md#run-from-a-repository-checkout).
The loading is in `environment.py`:

- `env/.env.connector`, `env/.env.parser` and `env/.env.models` are loaded on startup,
  the same files Compose hands to the worker. An exported or inline variable still wins
  over the file.
- `CONNECTOR_ENV_FILE`, `PARSER_ENV_FILE` and `MODEL_ENV_FILE` relocate those files,
  exactly as in `scripts/deployment/dev.sh`.
- `env/.env.api` and `env/.env.database` are not loaded wholesale: they point services at
  in-cluster hostnames and at the API's own control-plane DSN, which a host CLI cannot
  reach. From the checkout's `env/.env.database`, the CLI reads only the MinIO root pair
  (the host reaches the same MinIO on `localhost`) and an uncommented
  `HARBORRAG_CONTROL_DB_URL` line, with the `HARBORRAG_SECRETS_ENCRYPTION_KEY` that
  database's secrets are stored under. The `POSTGRES_*` values alone never select the
  database.
- `DATABASE_ENV_FILE` names another database file and is honoured in any directory.

## Point a checkout at the stack's control database

From a checkout, every ingest and connection write refuses the local SQLite control
database until `HARBORRAG_CONTROL_DB_URL` points at the stack's Postgres. The commands it
covers, the message and the fix are in
[Run from a repository checkout](../../../../../docs/users/cli-reference/README.md#run-from-a-repository-checkout).
The check is `checkout_write_refusal()` in `environment.py`.

Once the variable is set, every checkout command migrates that database, so rebuild the
images after each pull:

```bash
scripts/deployment/dev.sh up --build
```

## Address a connection

`ingest run|start CONNECTOR` takes the connection's `source_key`. How that key is formed,
and why a source type such as `local` or an imported key under another `--tenant` fails
with `ConnectionNotFoundError`, is in
[Name the connector](../../../../../docs/users/cli-reference/README.md#name-the-connector).
How `config/sources/connectors.yaml` is imported once into the control database is in
[Connector configuration](../../../../../docs/users/configuration/connector-config.md).

Three things the reference does not spell out:

- An imported entry whose variable is unset is stored with status `error` and an issue
  such as `environment variable JIRA_BASE_URL is not set`, so `harborrag source list`
  shows it among the connections needing attention.
- `--connection-id` is not `--connector-id`: it sets the logical connection identity of
  the run and defaults to the connector name.
- Credentials must also be present in the worker, which runs in its own container and
  does the fetching. Compose supplies them from `env/.env.connector` through `env_file:`,
  so a worker started before an edit keeps the old values until it restarts.

A zero exit from `ingest start` without `--wait` means the workflow was submitted, not
that it succeeded. Credentials and connectivity are only exercised once the run reaches
its fetch stage, so a bad token still submits cleanly and fails afterwards. Use `--wait`,
or check `harborrag ingest status RUN_ID`, before treating a run as done.

## Follow and control a run

`ingest watch`, `ingest start --wait` and `ingest run` draw the same inline Rich progress
block (`progress_view.py`, `following.py`). Polling, timeouts, Ctrl-C, pause, resume and
cancel behaviour are in
[Follow a run](../../../../../docs/users/cli-reference/README.md#follow-a-run).

Cancellation waits for a safe source-batch boundary, persists `CANCELLED` and drains
eligible projection cleanup jobs. Activity retries are automatic. To replay a terminal
failure, submit a new run for the same source scope; the pipeline resumes from raw,
canonical, chunk or projection artifacts when their fingerprints remain reusable.

## Diagnose a control-plane schema error

Commands that do real work refuse to start when the control-plane database is not
usable:

```text
Control plane is not ready. Refusing to run against a database whose schema may be stale; run 'harborrag doctor' for diagnostics.
```

`harborrag doctor` is exempt: it stays available precisely when the control plane is
degraded. The message deliberately leaves out the underlying database error. With
`--json` the data is `{"error_type": "ControlPlaneUnavailable"}` and nothing more; the
detail is logged, not returned, so read the process log (or run `harborrag doctor`) to
find out which migration failed. The check is in `runner.py`.

When the migrations themselves fail, composition stops with
`control-plane migrations failed; inspect the startup logs` and logs an `ERROR` line from
`harborrag.runtime.composition` whose message reads:

```text
Control-plane migrations failed database_scheme=... error_type=... hint=...
```

A `hint=` saying the schema exists but is not stamped means the schema was created
without Alembic recording it, so the runner replays from the first revision and collides
with `table <name> already exists`. Stamp the version table at the revision the schema
already matches, then upgrade:

```bash
uv run python - <<'PY'
from alembic import command
from harborrag_adapters.repositories.database.control_plane import migrations
cfg = migrations._build_config("sqlite+aiosqlite:///./harborrag_control.db")
command.stamp(cfg, "0007")
command.upgrade(cfg, "head")
PY
```

Replace `0007` with the revision the existing schema matches, and the URL with your
control database.

## Retrieve without leaking content

`retrieve` options and the two checkout variables it needs are in
[Retrieve evidence](../../../../../docs/users/cli-reference/README.md#retrieve-evidence).
Document content is left out of the output by default. Use `--include-content` only when
the terminal and downstream consumers may receive source text.

The CLI process must point at the same object-store root, active index generation,
collection, graph namespace and embedding space as the ingestion worker.

### Share one object store with the worker

Qdrant stores vectors and chunk references; the canonical chunk bodies live in the
S3-compatible object store. `retrieve` reads those bodies, so it must point at the store
the worker wrote them to.

The Compose worker uses `http://minio:9000` on the shared Docker network, while a CLI run
on the host defaults to `http://localhost:9000`: the same MinIO container through its
published port, so they agree by default. They diverge if you change `MINIO_API_PORT`,
bind MinIO to a different address, or run the CLI against a different deployment.

When they disagree, ingestion reports success and retrieval then fails on a missing
object even though the vectors are present. Point the CLI at the worker's endpoint with
`HARBORRAG_OBJECT_STORE_ENDPOINT_URL` (plus the matching credentials), or run `retrieve`
inside the worker's environment.

## Control output and logging

The JSON envelope and exit codes are in
[Read JSON output and exit codes](../../../../../docs/users/cli-reference/README.md#read-json-output-and-exit-codes).
Logs stay on stderr. Disable color globally with either form:

```bash
uv run harborrag --no-color ingest status RUN_ID
NO_COLOR=1 uv run harborrag ingest status RUN_ID
```

Set `HARBORRAG_LOG_LEVEL=INFO` or `DEBUG` for diagnostic logs. The CLI defaults to
`WARNING` so normal and JSON output stay concise.

## Configure the runtime

The CLI builds the production application composition for every invocation. These are
the `HARBORRAG_` variables it reads most often; every runtime variable is in the
[engine configuration reference](../../../../../docs/users/configuration/config-file-reference.md).

| Variable | Default | Used by |
| --- | --- | --- |
| `HARBORRAG_ENV` | `dev` | Runtime safety policy |
| `HARBORRAG_CONTROL_DB_URL` | local SQLite | Control-plane migrations, connections, run state and diagnostics |
| `HARBORRAG_TEMPORAL_CONFIG_PATH` | `config/temporal.yaml` | Temporal connection, queues, retries, worker, health and workflow configuration |
| `HARBORRAG_TEMPORAL_TARGET` | value from YAML | Optional `doctor --temporal` and ingestion override |
| `HARBORRAG_TEMPORAL_NAMESPACE` | value from YAML | Optional workflow namespace override |
| `HARBORRAG_TEMPORAL_TLS` | value from YAML | Optional TLS override for a managed Temporal endpoint |
| `HARBORRAG_TEMPORAL_API_KEY` | unset | Secret API credential; keep it outside tracked YAML |
| `HARBORRAG_MODEL_CONFIG_PATH` | `config/models/models.yaml` | Chat and query embedding configuration |
| `HARBORRAG_QDRANT_URL` | `http://localhost:6333` | Vector retrieval |
| `HARBORRAG_FALKORDB_HOST` | `localhost` | Graph expansion |
| `HARBORRAG_FALKORDB_PORT` | `6379` | Graph expansion |
| `HARBORRAG_OBJECT_STORE_ENDPOINT_URL` | `http://localhost:9000` | S3 or MinIO object store holding canonical chunk content |
| `HARBORRAG_OBJECT_STORE_ACCESS_KEY_ID` | unset | Object-store credential |
| `HARBORRAG_OBJECT_STORE_SECRET_ACCESS_KEY` | unset | Object-store credential |
| `HARBORRAG_STORAGE_NAMESPACE_PREFIX` | `harborrag` | Per-tenant store namespace `{prefix}_{tenant_id}`: Qdrant collection stem, FalkorDB graph, object-store key prefix (replaces the removed `HARBORRAG_QDRANT_COLLECTION_PREFIX`) |
| `HARBORRAG_FALKORDB_GRAPH` | `harborrag` | Graph projection name |
| `HARBORRAG_CORPUS_ACCESS_MODE` | `source_acl` | Whether retrieval reads the shared corpus or the caller's source permissions |
| `HARBORRAG_CORPUS_SHARED_TENANT_ID` | unset | Tenant whose retrieval reads the shared corpus in `tenant_shared` mode |
| `HARBORRAG_LOG_LEVEL` | `WARNING` for the CLI | Diagnostic logging |

Plaintext connections to non-loopback services fail closed unless their explicit
`HARBORRAG_*_ALLOW_INSECURE_REMOTE` development-network option is enabled. Do not use
those opt-outs on an untrusted network.

## Keep the package boundary

This package may:

- define Typer commands and validate command-line arguments;
- render `AppResponse` values with Rich;
- manage one application-service lifecycle per invocation;
- emit stable JSON and exit codes for automation.

It must not:

- call connectors, parsers, repositories or Temporal SDK handles directly;
- implement workflow or retrieval business rules;
- import FastAPI to provide CLI behaviour;
- expose secrets or unreviewed provider exception text.

Run its focused tests from the repository root:

```bash
uv run pytest \
  packages/harborrag-app/tests/test_ingestion_cli_rendering.py \
  packages/harborrag-app/tests/test_ingestion_service_cli.py \
  packages/harborrag-app/tests/test_retrieval_cli.py
```
