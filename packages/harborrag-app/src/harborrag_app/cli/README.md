# HarborRAG CLI

Find where each part of the `harborrag` command lives, how to add or change a command,
and the failure modes worth knowing before you change this package. The
`harborrag_app.cli` package is the operator command line: a second adapter, next to the
FastAPI routes, over the operations in `harborrag_app.operations`. A command parses its
input, calls one operation and prints the result; the operation holds the rules.

These notes are for contributors. What each command does for a user, how it maps to
HTTP, its global options, output and exit codes are in the
[CLI reference](../../../../../docs/users/cli-reference/README.md). Every option of
every command is in the generated
[CLI command reference](../../../../../docs/users/cli-reference/commands.md).

## Install and run from a checkout

The CLI runs from inside a HarborRAG repository checkout. Install the production
dependencies that ingestion and the local stack need:

```bash
uv sync --package harborrag-app --extra production
uv run harborrag --help
```

The base package is enough for `--help`. A command that calls an operation builds the
whole application service in its process, so it needs the services that service uses:
the control database, Temporal for ingestion runs, and the stores and the model gateway
for `chat`. Start the repository's development stack with:

```bash
scripts/deployment/dev.sh up
```

The non-root CLI image, `deploy/docker/Dockerfile.cli`, is built and run as described in
[Build the CLI and MCP images](../../../../../docs/developers/deployment/README.md#build-the-cli-and-mcp-images).

## Find the code

`main.py` registers the commands and resolves the checkout or project directory before
any command builds a service. The kernel modules every command shares:

| Module | What it owns |
| --- | --- |
| `globals.py` | The global options (`--tenant`, `-o/--output`, `--profile`, `--config`, `-y/--yes`) and their precedence: flag, environment, profile, default |
| `profiles.py` | The profile file `~/.config/harborrag/config.yaml`, with the keys `tenant` and `output` only |
| `invoke.py` | `run_operation`: builds the service, runs the checkout refusal and the tenant question before a write, calls the operation, prints the body or the error, and exits with the spec's code; holds the CLI's log records back until the command succeeds |
| `output.py` | Prints a body as JSON, YAML or a table, and an error as the HTTP envelope or one line on stderr |
| `views.py` | The table views: list columns, resource trees and the `doctor` report |
| `exit_codes.py` | The exit code of each HTTP status, plus usage, failed run and Ctrl-C |
| `bodies.py` | Body files (`-f`, `-` for stdin) merged with flags, validated into the request model; `--secret-prompt` |
| `paging.py` | `--limit`, `--cursor` and `--all` |
| `tenant_guard.py` | The question before a write into a tenant with no data, and before a deletion |
| `local_mode.py` | The local-mode principal, `cli:<os-user>` with role `owner` in every tenant |
| `follow.py` | `--wait` and `--watch`: reads a run every 5 seconds until it ends; a read that fails reports the error with the run's ID and how to re-attach |
| `inline_run.py` | `ingestions start --inline`: runs the pipeline in this process without Temporal |
| `environment.py` | Loads the checkout's `env/` files and refuses writes to the local SQLite control database |
| `project.py` | Finds the checkout or a `harborrag.yaml` project directory |
| `registry.py` | Which operation each command calls, and which commands and operations have no counterpart |
| `reference.py` | Renders `docs/users/cli-reference/commands.md` from the command tree, the registry and the OpenAPI document `make openapi` exports: each command's HTTP route, the CLI-only and HTTP-only lists, and every command's options |

One module per command group in `commands/`:

| Command | Subcommands | Module |
| --- | --- | --- |
| `harborrag me` | none | `commands/me.py` |
| `harborrag projects` | `list`, `get`, `create`, `update`, `delete` | `commands/projects.py` |
| `harborrag source-types` | `list` | `commands/source_types.py` |
| `harborrag connections` | `list`, `get`, `create`, `update`, `delete`, `test` | `commands/connections.py` |
| `harborrag ingestions` | `start`, `list`, `get`, `pause`, `resume`, `cancel`, `retry-failures` | `commands/ingestions.py` |
| `harborrag failures` | `list`, `retry` | `commands/failures.py` |
| `harborrag settings` | `schema`, `get`, `set` | `commands/settings.py` |
| `harborrag chat` | none | `commands/chat.py` |
| `harborrag config` | `import`, `export` | `commands/config.py` |
| `harborrag doctor` | none | `commands/doctor.py`, `doctor/` |
| `harborrag auth keys` | `create`, `list`, `revoke` | `commands/auth.py`, `commands/auth_keys.py` |
| `harborrag source`, `harborrag ingest` (hidden) | the old names | `commands/legacy.py` |

The legacy stubs accept any arguments, `--help` and `-h` included, print the command that
replaced the old one, such as
`error: 'harborrag source list' was renamed; use 'harborrag connections list'`, and exit
2. A group alone, with or without `--help`, names the group that replaced it:
`error: 'harborrag ingest' was renamed; use 'harborrag ingestions'`. `retrieve` and
`topology` are not registered at all.

## Add or change a command

1. Write the command in its group's module. It takes the global options as the
   `TenantOption`, `OutputOption`, `ProfileOption`, `ConfigOption` and `YesOption`
   parameters and resolves them with `current_globals`.
2. Parse the arguments and build the request body there, with `parse_ref` for a
   connection ref and `load_body` for a body; a parse problem is a `click.UsageError`
   (exit 2).
3. Define `call(operations, scope)`, which calls one method of `LocalOperations`, and
   pass it to `run_operation` with the operation's ID. Pass `write=True` when the
   operation writes, so the checkout refusal and the tenant question run.
4. Add the command to `COMMAND_OPERATIONS` in `registry.py`, or to `CLI_ONLY` with the
   reason when it has no operation.
5. Regenerate the command reference, which exports the OpenAPI document first, and commit
   it with the change:

    ```bash
    make cli-reference
    ```

6. Update the [CLI reference](../../../../../docs/users/cli-reference/README.md) when
   the command changes how the CLI maps to HTTP.

Keep business rules in the operations: the same method serves the HTTP route, so a rule
written in a command is a rule HTTP does not apply. Nothing in this package or in
`harborrag_app.operations` may import FastAPI or Starlette, so the CLI runs without
them.

## Keep the CLI in step with HTTP

`tests/test_cli_http_parity.py` fails the build when the two drift apart:

- every `operationId` of the OpenAPI export is a command's operation in
  `COMMAND_OPERATIONS` or listed in `HTTP_ONLY`, and every command maps to an operation,
  is in `CLI_ONLY` or is a legacy stub;
- a command's `-o json` output validates against its operation's response schema;
- a failing call answers the same status and code as the route, and the exit code the
  table in `exit_codes.py` gives;
- `docs/users/cli-reference/commands.md` equals what `make cli-reference` writes.

`tests/test_cli_no_fastapi.py` checks that the CLI imports without FastAPI. Run the CLI
tests from the repository root:

```bash
uv run pytest packages/harborrag-app/tests -k "cli or doctor" -q
```

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
  (the host reaches the same MinIO on `localhost`), `HARBORRAG_LOCAL_SOURCE_ROOT`, and an
  uncommented `HARBORRAG_CONTROL_DB_URL` line, with the
  `HARBORRAG_SECRETS_ENCRYPTION_KEY` that database's secrets are stored under. The
  `POSTGRES_*` values alone never select the database.
- `DATABASE_ENV_FILE` names another database file and is honoured in any directory.

From a checkout, every write refuses the local SQLite control database until
`HARBORRAG_CONTROL_DB_URL` points at the stack's Postgres. The check is
`checkout_write_refusal()` in `environment.py`. Once the variable is set, every checkout
command migrates that database, so rebuild the images after each pull:

```bash
scripts/deployment/dev.sh up --build
```

## Diagnose a control-plane schema error

A command that calls an operation refuses to start when the control-plane database is
not usable, and exits 6:

```text
error 503 dependency_unavailable: Control plane is not ready. Refusing to run against a database whose schema may be stale; run 'harborrag doctor' for diagnostics. (set HARBORRAG_LOG_LEVEL=DEBUG for details) (trace 1a2b3c4d…)
```

`harborrag doctor` stays available precisely when the control plane is degraded. The
message leaves out the underlying database error; run with `HARBORRAG_LOG_LEVEL=DEBUG`,
or run `harborrag doctor`, to see which migration failed. The check is
`control_plane_failure()` in `invoke.py`.

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

## Control logging

Logs go to stderr at `WARNING` by default. While an operation runs, `invoke.py` holds
the CLI's log records back, prints them after a success and drops them after a failure,
so a failure's stderr is its error report alone, as over HTTP. Set
`HARBORRAG_LOG_LEVEL=INFO` or `DEBUG` to turn the hold off; `DEBUG` also prints the
cause and traceback of a failure as it happens. Disable color with either form:

```bash
uv run harborrag --no-color doctor
NO_COLOR=1 uv run harborrag doctor
```

## Configure the runtime

The CLI builds the production application composition for every invocation. These are
the `HARBORRAG_` variables it reads most often; every runtime variable is in the
[engine configuration reference](../../../../../docs/users/configuration/config-file-reference.md).

| Variable | Default | Used by |
| --- | --- | --- |
| `HARBORRAG_ENV` | `dev` | Runtime safety policy |
| `HARBORRAG_TENANT_ID` | `DEFAULT` | The CLI's default tenant, shared with the API and the workers |
| `HARBORRAG_CONTROL_DB_URL` | local SQLite | Control-plane migrations, connections, run state and diagnostics |
| `HARBORRAG_LOCAL_SOURCE_ROOT` | unset | The folder `local_file` connections must stay inside |
| `HARBORRAG_TEMPORAL_CONFIG_PATH` | `config/temporal.yaml` | Temporal connection, queues, retries, worker, health and workflow configuration |
| `HARBORRAG_TEMPORAL_TARGET` | value from YAML | Optional Temporal address override for `doctor` and ingestion |
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
| `HARBORRAG_STORAGE_NAMESPACE_PREFIX` | `harborrag` | Per-tenant store namespace `{prefix}_{tenant_id}`: Qdrant collection stem, FalkorDB graph, object-store key prefix |
| `HARBORRAG_FALKORDB_GRAPH` | `harborrag` | Graph projection name |
| `HARBORRAG_CORPUS_ACCESS_MODE` | `source_acl` | Whether chat retrieval reads the shared corpus or the caller's source permissions |
| `HARBORRAG_CORPUS_SHARED_TENANT_ID` | unset | Tenant whose retrieval reads the shared corpus in `tenant_shared` mode |
| `HARBORRAG_LOG_LEVEL` | `WARNING` for the CLI | Diagnostic logging |

`chat` reads its passages from the same object store the worker wrote them to. The
Compose worker uses `http://minio:9000` on the shared Docker network, while the CLI on
the host defaults to `http://localhost:9000`: the same MinIO container through its
published port. They diverge if you change `MINIO_API_PORT` or run the CLI against
another deployment; then point `HARBORRAG_OBJECT_STORE_ENDPOINT_URL` at the worker's
store.

Plaintext connections to non-loopback services fail closed unless their explicit
`HARBORRAG_*_ALLOW_INSECURE_REMOTE` development-network option is enabled. Do not use
those opt-outs on an untrusted network.

## Keep the package boundary

This package may:

- define Typer commands and parse command-line arguments and body files;
- call the operations through `run_operation`, one application service per invocation;
- print bodies and errors in the shape the HTTP route answers, and exit with the codes
  in `exit_codes.py`.

It must not:

- call connectors, parsers, repositories or Temporal SDK handles directly;
- hold business rules that the HTTP route would not apply;
- import FastAPI or Starlette;
- print secrets, document content or unreviewed provider exception text.
