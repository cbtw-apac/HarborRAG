# Running HarborRAG from a checkout

This page is for people working on HarborRAG itself, or running the durable Temporal-based
ingestion mode. If you just want to use HarborRAG on your documents, follow the
[Quick Start](../getting-started/quick-start.md) instead - it needs no clone.

Two stages: a five-minute check that needs no Docker or credentials, then the full local
stack (data services, Temporal, worker, API) for durable ingestion and the HTTP surface.

## Requirements

- Python 3.12 or newer
- [`uv`](https://docs.astral.sh/uv/)
- Docker Engine with Compose v2 (the full stack only)
- Roughly 520 MB of disk for the workspace sync, plus several GB for the full stack's images and
  model cache

## Explore a checkout without services

### 1. Clone and sync

```bash
git clone https://github.com/cbtw-apac/HarborRAG.git
cd HarborRAG
uv sync --all-packages --extra dev
```

### 2. Confirm the CLI

```bash
uv run harborrag --help
```

You should see nine commands, grouped by purpose: `init` (Setup); `doctor`, `auth` and
`config` (Operations); `chat` (Chat); `retrieve` (Retrieval); and `ingest`, `topology` and
`source` (Ingestion). This works without Temporal or any data service.

> `harborrag doctor` checks the checkout's catalogs and the local services, so most lines
> fail until step 6 starts them; add `--temporal` after step 7 to check the workflow service.

### 3. Parse a document

The parser registry picks a parser *family* by suffix and MIME type, then the family routes
to a concrete engine. Run this from the checkout root - `source_uri` is
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

Expect something like `markup markdown 663` - the family, the engine, and the extracted
character count.

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

Three connections are enabled: `harborrag-workspace` (local files, no credentials),
`confluence-main`, and `jira-main`. Only the first works without credentials, and it is the
one the rest of this guide uses.

That is the service-free check. You have a working install and a working parser. Continue below for
real ingestion, or jump to [Configuration](../users/configuration/README.md) to point
HarborRAG at your own sources.

## Run the full stack

### 5. Create the `env/` folder

Every credential lives in an ignored `env/` folder that you create once. One command
creates all seven files from their templates, generates a random MCP bearer token and the
control database's `HARBORRAG_SECRETS_ENCRYPTION_KEY`, and sets mode `0600`:

```bash
scripts/deployment/dev.sh bootstrap
```

Back up `HARBORRAG_SECRETS_ENCRYPTION_KEY` from `env/.env.database`: stored connection
secrets cannot be read without it. Then set a database password and uncomment the
`HARBORRAG_CONTROL_DB_URL` line that the template ships commented out:

```dotenv
# env/.env.database
POSTGRES_PASSWORD=<a strong password, replacing change-me>
HARBORRAG_CONTROL_DB_URL="postgresql+asyncpg://${POSTGRES_USER}:${POSTGRES_PASSWORD}@127.0.0.1:${POSTGRES_PORT:-5432}/${POSTGRES_DB}"
```

`HARBORRAG_CONTROL_DB_URL` points the host CLI at the stack's Postgres instead of a local
SQLite file. The line resolves the `POSTGRES_*` values and substitutes them verbatim, so
URL-encode a password that contains `@ : / # % ?`. When the CLI uses the line, it also takes
`HARBORRAG_SECRETS_ENCRYPTION_KEY` from the same file. Without the line, step 8 stops with
`Refusing to ingest or change connections from a repo checkout in the local sqlite control DB...`.

Every CLI command run from the checkout migrates that database. After each pull, rebuild the
images with `scripts/deployment/dev.sh up --build` before you run a CLI command, or the CLI
can migrate the database ahead of the running services.

```dotenv
# env/.env.models - config/models/models.yaml requires all five
HARBORRAG_URL=https://gateway.example.com
HARBORRAG_API_KEY=<your gateway key>
HARBORRAG_CHAT_MODEL=chat-model-name
HARBORRAG_EMBED_MODEL=embedding-model-name
HARBORRAG_RERANKER_MODEL=rerank-model-name
```

The checked-in model catalog sends chat, embedding, and reranking through one LiteLLM
proxy gateway. `HARBORRAG_URL` is its base URL without `/v1`, and the three model
values are the gateway's model names, as `GET <HARBORRAG_URL>/v1/models` lists them.
To call a provider directly instead, see
[Model configuration](../users/configuration/model-config.md).

Everything else has a working local default. See
[Configuration](../users/configuration/README.md) for what each file holds and how to keep
it outside the checkout.

Check the model catalog resolves before starting anything:

```bash
uv run --env-file env/.env.models \
  python -m harborrag_adapters.models explain config/models/models.yaml --family chat
```

References expand eagerly, so this fails immediately on a missing or empty variable -
which is exactly what you want to find out now rather than mid-ingestion.

### 6. Start the services

```bash
scripts/deployment/dev.sh up
```

That starts, in order: the data stack (PostgreSQL, Qdrant, FalkorDB, Redis, MinIO), the
Temporal server with its schema, namespace, and UI, the ingestion workers, and the
control-plane API.

On a clean checkout `up` bootstraps `env/` and then stops so you can review the
credentials. Run it again after editing them.

The first worker build installs the selected parser and model dependencies and can take
several minutes. Downloaded Hugging Face assets persist in the shared
`harborrag-model-cache` volume and are reused after the container is replaced.

To bring things up one at a time, use these commands:

| Command | Starts |
| --- | --- |
| `dev.sh data` | data services only - creates the shared network, so it goes first |
| `dev.sh temporal` | Temporal server, schema, namespace, UI - never a worker |
| `dev.sh worker` | the ingestion worker only |
| `dev.sh api` | the API only |
| `dev.sh up --no-worker` | everything except the worker |
| `dev.sh down` | stop everything; `--volumes` also discards the data |

Add `--build` to `up`, `worker`, or `api` after changing source or dependencies.
`config/` is mounted, so a configuration change needs only a restart.

**If `up` fails with `HARBORRAG_SECRETS_ENCRYPTION_KEY is blank in env/.env.database, but
… already holds a database`**, restore the key that database was written with. `dev.sh`
generates a new key only while no database volume exists, because a new key cannot read
secrets stored under the old one.

### 7. Check that the services are reachable

```bash
HARBORRAG_TEMPORAL_TARGET=localhost:7233 \
  uv run harborrag doctor --json --temporal
```

`doctor` only contacts Temporal when `--temporal` is passed; without the flag the target
above is read but never used, and the `temporal` check reports `skip`.

The Temporal UI is at <http://localhost:8080>, and `dev.sh api` prints the API health URL
(`http://127.0.0.1:8000/healthz` by default).

### 8. Ingest the workspace connection

```bash
export HARBORRAG_TEMPORAL_TARGET=localhost:7233
uv run harborrag ingest start harborrag-workspace --wait
```

The positional argument (`CONNECTOR` in `--help`) is the connection's `source_key`, not a
source type: `ingest start local` fails with `ConnectionNotFoundError`. The connector can
also be passed as `--connector-id`, or its alias `--connector`. On its first start the API
imports `config/sources/connectors.yaml` once into tenant `DEFAULT`, project `default`, and
keeps the keys `harborrag-workspace`, `confluence-main`, and `jira-main`.

Some connections have a `source_key` of the form `PROJECT/CONNECTION` instead: those on a
stack whose connections existed before migration 0041, and any connection created through
the API or `harborrag source add`. If the command above fails with `ConnectionNotFoundError`,
use that form:

```bash
uv run harborrag ingest start default/harborrag-workspace --wait
```

For a quick trial, add `--limit 3` to stop after three discovered artifacts.

`--wait` shows the same inline progress block as `ingest run`; `ingest watch RUN_ID`
attaches it to a run started elsewhere.

Instead of `ingest start`, you can run `source sync`. It addresses the project and the
connection rather than the `source_key`, so it works on any stack. It runs the same checks
as `POST /v1/admin/ingestions` and prints the `harborrag ingest watch` command that follows
the new run:

```bash
uv run harborrag source sync default/harborrag-workspace
```

Save the run ID and drive it:

```bash
uv run harborrag ingest status RUN_ID --json
uv run harborrag ingest wait RUN_ID --json
uv run harborrag ingest watch RUN_ID      # inline progress until the run settles
uv run harborrag ingest pause RUN_ID
uv run harborrag ingest resume RUN_ID
uv run harborrag ingest cancel RUN_ID
```

Re-running the same ingestion reports documents as `unchanged`. That is the admission fast
path, not an error - it skips parsing, chunking, and encoding. Use `--force-reprocess` to
override it; see [Ingestion modes](../users/ingestion-modes.md).

### 9. Retrieve and chat over the ingested documents

The worker reads the shared-corpus settings from `env/.env.temporal`, which the CLI does not
load. Export them in the shell you query from, or retrieval returns no results:

```bash
export HARBORRAG_CORPUS_ACCESS_MODE=tenant_shared
export HARBORRAG_CORPUS_SHARED_TENANT_ID=DEFAULT
```

```bash
uv run harborrag retrieve "deployment requirements" \
  --top-k 5 \
  --include-content
```

`retrieve` runs hybrid dense + sparse search with graph observation enabled. Use
`--lane dense|sparse|hybrid` to pin one lane, `--mode local_semantic` for bounded semantic
expansion instead of the default `flat`, and `--no-graph` to skip graph expansion.

Chat is retrieval-grounded and makes a real provider request, so it may incur charges:

```bash
uv run harborrag chat \
  "Explain HarborRAG in one paragraph." \
  --json
```

The JSON response contains a generated session ID. Pass it back with `--session` on later
calls to keep conversation context. See the [Chat guide](../users/chat/README.md) for the
HTTP surface.

### 10. Connect an MCP client

```bash
scripts/deployment/mcp.sh --check
```

That performs a real MCP handshake and prints the eleven advertised retrieval tools without
opening provider connections. For normal use, point your MCP client at
`scripts/deployment/mcp.sh`; for a browser playground, run `scripts/deployment/mcp.sh --http`
and open <http://127.0.0.1:8010/>. See
[MCP Tools](../users/detailed-guides/mcp-server/README.md).

## Run the tests

```bash
uv run pytest packages/harborrag-core/tests   # one package, fast
uv run pytest                                 # everything
uv run make coverage                          # with the 90% gate
```

`--extra dev` covers most of the suite, but several packages gate optional adapters behind
their own extras. To run it the way CI does:

```bash
uv sync --all-packages --all-extras
uv run pytest
```

## Get help when something fails

- [Troubleshooting](../users/troubleshooting/README.md) - setup, configuration, provider,
  and quality-gate failures
- [Installation](../getting-started/installation.md) - `pip`, editable installs, PDF/OCR
  backends, platform notes

## Read next

- [Configuration](../users/configuration/README.md) - point HarborRAG at your own sources
- [CLI Reference](../users/cli-reference/README.md) - every command and flag
- [Python SDK](../users/python-sdk/README.md) - use it as a library
- [Chat](../users/chat/README.md) - retrieval-grounded chat over HTTP and CLI
- [Architecture](architecture/README.md) - before contributing code
- [Deployment](deployment/README.md) - the full local topology and the
  production boundary
