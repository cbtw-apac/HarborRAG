# Running HarborRAG from a checkout

Work on HarborRAG itself once the [Quick Start](../getting-started/quick-start.md) has your
checkout and its local stack running. This page shows how to explore the code without
services, start and rebuild one service at a time, drive ingestion runs, and run the tests.

Run every command from the root of the checkout unless a step says otherwise.

## Requirements

The [Quick Start prerequisites](../getting-started/quick-start.md#before-you-start) cover what
this page needs: Git, uv, Docker Engine with Compose v2, OpenSSL and a LiteLLM proxy gateway.
The workspace pins Python 3.12 in `.python-version`.

## Explore a checkout without services

### 1. Install the development tools

[Quick Start step 1](../getting-started/quick-start.md#1-clone-the-repository-and-install-the-cli)
clones the repository and installs the CLI. To change code, also install the `dev` extra,
which adds the lint, type-check and test tools:

```bash
uv sync --all-packages --extra dev --extra production
```

The environment takes about 930 MB. `--extra dev` on its own leaves out the Temporal,
PostgreSQL, Qdrant, FalkorDB and MinIO clients that the stack commands below need.

### 2. Confirm the CLI

`uv run harborrag --help` lists the commands described in Quick Start step 1, and works
without Temporal or any data service. The `harborrag` command runs only inside a repository
checkout: elsewhere it stops with `harborrag: no project found. Run it inside a HarborRAG
repository checkout, ...`.

`harborrag doctor` checks the checkout's catalogs and the local services, Temporal included,
so its service lines fail until step 6 starts them.

### 3. Parse a document

The parser registry picks a parser *family* by suffix and MIME type, then the family routes
to a concrete engine. Run this from the checkout root, because `source_uri` is
repository-relative:

```bash
uv run python - <<'PY'
import asyncio

from harborrag_adapters.parsers import HarborParserFactory, ParseRequest


async def main() -> None:
    registry = HarborParserFactory().create_registry()
    result = await registry.parse_request(
        ParseRequest(
            source_uri="docs/getting-started/README.md",
            filename="README.md",
            mime_type="text/markdown",
        )
    )
    print(result.parser_name, result.engine_name, len(result.text))


asyncio.run(main())
PY
```

You should see a line such as `markup markdown 2938`: the family, the engine, and the number
of characters extracted, which changes whenever the file does.

### 4. Inspect the catalogs

The checked-in YAML files load through the runtime loaders, which is the fastest way to see
what a fresh checkout is configured to do:

```bash
LOCAL_SOURCE_PATH=docs uv run python -c "
from harborrag_runtime.config import load_connector_catalog
c = load_connector_catalog('config/sources/connectors.yaml')
print('enabled connectors:', c.names(enabled_only=True))
"

uv run python -c "
from harborrag_runtime.config import load_parser_catalog
c = load_parser_catalog('config/ingestion/parsers.yaml')
print('enabled parsers:', c.names(enabled_only=True))
"
```

Four connections are enabled: `harborrag-workspace` (local files, no credentials),
`confluence-main`, `jira-main` and `sharepoint-main`. Only the first works without
credentials, and it is the one the rest of this page uses. The enabled parsers are
`image-liteparse` and `pdf-liteparse`.

Continue below for the full stack, or see [Configuration](../users/configuration/README.md)
to point HarborRAG at your own sources.

## Run the full stack

### 5. Create the `env/` folder

[Quick Start step 2](../getting-started/quick-start.md#2-create-the-environment-files)
creates `env/` with `scripts/deployment/dev.sh bootstrap`, fills in the five model gateway
values in `env/.env.models`, and uncomments the `HARBORRAG_CONTROL_DB_URL` line in
`env/.env.database`. When you work on the code, these points also matter:

- Back up `HARBORRAG_SECRETS_ENCRYPTION_KEY` from `env/.env.database`. The API and the worker
  encrypt stored connection secrets with it, and cannot read them without it.
- The `HARBORRAG_CONTROL_DB_URL` line substitutes the `POSTGRES_*` values verbatim, so
  URL-encode a password that contains `@ : / # % ?`. When the CLI uses the line, it also takes
  `HARBORRAG_SECRETS_ENCRYPTION_KEY` from the same file.
- Every CLI command run from the checkout migrates that database. After each pull, rebuild
  the images with `scripts/deployment/dev.sh up --build` before you run a CLI command, or the
  CLI can migrate the database ahead of the running services.

Check that the model catalog resolves before you start anything:

```bash
uv run --env-file env/.env.models \
  python -m harborrag_adapters.models explain config/models/models.yaml --family chat
```

It prints the chat family as JSON. References expand eagerly, so an unset variable fails here
with `environment variable <NAME> is not set` instead of in the middle of an ingestion.

[Deployment](deployment/README.md#prepare-the-environment-files) lists which service reads
which file, and [Configuration](../users/configuration/README.md) explains how to keep them
outside the checkout.

### 6. Start the services

`scripts/deployment/dev.sh up` starts the whole stack, as in
[Quick Start step 3](../getting-started/quick-start.md#3-start-the-local-stack). On a clean
checkout `up` bootstraps `env/` and then stops, so you can review the credentials; run it again
after editing them.

To bring things up one at a time, use these commands:

| Command | Starts |
| --- | --- |
| `dev.sh data` | data services only; creates the shared network, so it goes first |
| `dev.sh temporal` | Temporal server, schema, namespace and UI; never a worker |
| `dev.sh worker` | the ingestion worker only |
| `dev.sh api` | the API only |
| `dev.sh up --no-worker` | everything except the worker |
| `dev.sh down` | stops everything; `--volumes` also discards the data |
| `dev.sh sharepoint-login` | signs the worker in to SharePoint for `auth_mode: device_code` |

Add `--build` to `up`, `worker` or `api` after changing source or dependencies. `config/` is
mounted into the containers, so a configuration change needs only a restart. Add
`--device gpu` to `up` or `worker` to build the CUDA worker image, which needs the NVIDIA
Container Toolkit on the host.

The first worker build installs the selected parser and model dependencies and can take
several minutes. Downloaded Hugging Face assets persist in the shared `harborrag-model-cache`
volume and are reused after the container is replaced.

**If `up` fails with `HARBORRAG_SECRETS_ENCRYPTION_KEY is blank in env/.env.database, but …
already holds a database`**, restore the key that database was written with. `dev.sh`
generates a new key only while no database volume exists, because a new key cannot read
secrets stored under the old one.

### 7. Check that the services are reachable

```bash
uv run harborrag doctor
```

`doctor` prints one line per check and ends with the tenants that hold data. It exits 0 when
every required check passes, 6 when only service checks fail, and 1 when a configuration
check fails; `-o json` prints the same report as JSON. In a checkout, the `source path` and
`connector credentials` checks fail until `env/.env.connector` sets `LOCAL_SOURCE_PATH` and
the credentials of the enabled remote connections. The stack does not need them for
`harborrag-workspace`: `dev.sh` mounts `docs/` into the worker when `LOCAL_SOURCE_PATH` is
unset.

The Temporal UI is at <http://localhost:8080>, and `dev.sh api` prints the API health URL,
`http://127.0.0.1:8000/healthz` by default.

### 8. Ingest the workspace connection

[Quick Start step 5](../getting-started/quick-start.md#5-ingest-the-documentation-folder)
runs `uv run harborrag ingestions start default/harborrag-workspace --wait`. The argument is
the connection's ref, `PROJECT_ID/CONNECTION_ID`, the same value as `connection_ref` in the
HTTP API. On its first start the API imports `config/sources/connectors.yaml` once into
tenant `DEFAULT`, project `default`, and each YAML key becomes a `connection_id`. A bare key
such as `harborrag-workspace`, or a source type such as `local`, is refused with a hint;
`uv run harborrag connections list` lists the refs.

The CLI reaches Temporal at `localhost:7233` through `config/temporal.yaml`. Do not export
`HARBORRAG_TEMPORAL_TARGET` in the shell you start the stack from: Compose reads it too and
would point the containers at their own `localhost`.

`--wait` prints a line whenever the run's status or counters change, and exits 0 when the run
succeeds and 8 when it fails or is cancelled. Without `--wait`, the command prints the started
run and returns; follow it from any shell with its `ingestion_id`:

```bash
uv run harborrag ingestions list --connection-ref default/harborrag-workspace
uv run harborrag ingestions get INGESTION_ID
uv run harborrag ingestions get INGESTION_ID --watch
```

Pause, resume or cancel a run with the same ID:

```bash
uv run harborrag ingestions pause INGESTION_ID
uv run harborrag ingestions resume INGESTION_ID
uv run harborrag ingestions cancel INGESTION_ID
```

Ctrl-C stops following and leaves the run going.

`--inline` runs the pipeline in the CLI's own process, without Temporal or a worker, which
helps when you debug the pipeline. It reads the files on this machine, so it needs a
connection whose `source_path` is a directory here, inside the CLI's
`HARBORRAG_LOCAL_SOURCE_ROOT` (`/data/sources` in the `env/.env.database` template). The
stack's `default/harborrag-workspace` names `/data/sources` as the containers see it, so
on a machine without that directory `--inline` refuses it with `422 validation_error`.
See [Run an ingestion in this process](../users/cli-reference/README.md#run-an-ingestion-in-this-process).

Re-running the same ingestion counts the unchanged documents as `skipped`. That is the
admission fast path, not an error: it skips parsing, chunking and encoding. Use
`--mode force` to override it; see [Ingestion modes](../users/ingestion-modes.md).

### 9. Chat over the ingested documents

[Quick Start step 6](../getting-started/quick-start.md#6-ask-a-question) exports
`HARBORRAG_CORPUS_ACCESS_MODE=tenant_shared` and `HARBORRAG_CORPUS_SHARED_TENANT_ID=DEFAULT`,
then runs `chat`. Without those exports, chat from a checkout finds no passages.

Chat is retrieval-grounded and makes a real provider request, so it can incur charges. It
asks a one-off question, which stores nothing; `-o json` prints the HTTP completion body:

```bash
uv run harborrag chat "Explain HarborRAG in one paragraph." -o json
```

To keep a conversation's context, create a session over HTTP and pass its ID with
`--session-id` on each question. See the [Chat guide](../users/chat/README.md).

### 10. Connect an MCP client

```bash
scripts/deployment/mcp.sh --check
```

That performs a real MCP handshake and prints the eleven advertised retrieval tools without
opening provider connections. For normal use, point your MCP client at
`scripts/deployment/mcp.sh`; for a browser playground, run `scripts/deployment/mcp.sh --http`
and open <http://127.0.0.1:8010/>. See [MCP Tools](../users/detailed-guides/mcp-server/README.md).

## Run the tests

```bash
uv run pytest packages/harborrag-core/tests
uv run pytest
uv run make coverage
```

The first command runs one package's tests, the second the whole suite, and the third the
suite with the 90% coverage gate.

`--extra dev` covers most of the suite, but several packages gate optional adapters behind
their own extras. To run it the way CI does:

```bash
uv sync --all-packages --all-extras
uv run pytest
```

## Get help when something fails

- [Troubleshooting](../users/troubleshooting/README.md) - setup, configuration, provider and
  quality-gate failures.
- [Installation](../getting-started/installation.md) - uv and pip workflows, extras, PDF and
  OCR backends.

## Read next

- [Configuration](../users/configuration/README.md) - point HarborRAG at your own sources.
- [CLI Reference](../users/cli-reference/README.md) - how the commands map to the HTTP API,
  with every command and option.
- [Python SDK](../users/python-sdk/README.md) - use HarborRAG as a library.
- [Chat](../users/chat/README.md) - retrieval-grounded chat over HTTP and CLI.
- [Architecture](architecture/README.md) - read before contributing code.
- [Deployment](deployment/README.md) - the full local topology and the production boundary.
