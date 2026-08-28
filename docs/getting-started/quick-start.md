# Quick Start

Checkout to first ingested document. Two paths:

- **[Path A: no services](#path-a-explore-without-any-services)** - 5 minutes, no Docker,
  no credentials. Confirms the install and shows the parser working.
- **[Path B: the full stack](#path-b-run-the-full-stack)** - 20–40 minutes, needs Docker.
  Real ingestion, retrieval, and chat.

Start with A even if you want B; it catches install problems before Docker enters the
picture.

## Requirements

- Python 3.12 or newer
- [`uv`](https://docs.astral.sh/uv/)
- Docker Engine with Compose v2 (Path B only)
- Roughly 520 MB of disk for the workspace sync, plus several GB for Path B's images and
  model cache

## Path A: explore without any services

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

You should see four commands, grouped by purpose: `doctor` (Operations), `chat` (Chat),
`retrieve` (Retrieval), and `ingest` (Ingestion, itself a subgroup). This works without
Temporal or any data service.

> `harborrag doctor` is a **live Temporal health check**, so it will fail here. Run it
> after step 7.

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
c = load_connector_catalog('config/connectors.yaml')
print('enabled connectors:', c.names(enabled_only=True))
"

uv run python -c "
from harborrag_runtime.config import load_parser_catalog
c = load_parser_catalog('config/parsers.yaml')
print('enabled parsers:', c.names(enabled_only=True))
"
```

Three connections are enabled: `harborrag-workspace` (local files, no credentials),
`confluence-main`, and `jira-main`. Only the first works without credentials, and it is the
one the rest of this guide uses.

That is Path A. You have a working install and a working parser. Continue to Path B for
real ingestion, or jump to [Configuration](../users/configuration/README.md) to point
HarborRAG at your own sources.

## Path B: run the full stack

### 5. Create the `env/` folder

Every credential lives in an ignored `env/` folder that you create once. One command
creates all seven files from their templates, generates a random MCP bearer token, and sets
mode `0600`:

```bash
scripts/deployment/dev.sh bootstrap
```

Now fill in the values that have no safe default. **The first one is mandatory - Compose
will not start without it:**

```bash
openssl rand -hex 32     # use the output as HARBORRAG_SECRETS_ENCRYPTION_KEY
```

```dotenv
# env/.env.database
HARBORRAG_SECRETS_ENCRYPTION_KEY=<the value above>
POSTGRES_PASSWORD=<a strong password, replacing change-me>
```

```dotenv
# env/.env.models - config/models.yaml requires all six
HARBOR_CHAT_PROVIDER=openai
HARBOR_CHAT_MODEL=gpt-4o-mini
HARBOR_CHAT_API_KEY=<your key>
HARBOR_EMBED_PROVIDER=openai
HARBOR_EMBED_MODEL=text-embedding-3-small
HARBOR_EMBED_API_KEY=<your key>
```

Everything else has a working local default. See
[Configuration](../users/configuration/README.md) for what each file holds and how to keep
it outside the checkout.

Check the model catalog resolves before starting anything:

```bash
uv run --env-file env/.env.models \
  python -m harborrag_adapters.models explain config/models.yaml --family chat
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

Prefer to bring things up one at a time?

| Command | Starts |
| --- | --- |
| `dev.sh data` | data services only - creates the shared network, so it goes first |
| `dev.sh temporal` | Temporal server, schema, namespace, UI - never a worker |
| `dev.sh worker` | the ingestion worker only |
| `dev.sh api` | the API only |
| `dev.sh up --no-worker` | everything except the worker |
| `dev.sh down` | stop everything; `--volumes` also discards the data |

Add `--build` to `up`, `worker`, or `api` after changing source, dependencies, or baked
worker configuration.

**If `up` fails with `required variable HARBORRAG_SECRETS_ENCRYPTION_KEY is missing a
value`**, go back to step 5 - Compose treats an empty value as unset.

### 7. Verify

```bash
HARBORRAG_TEMPORAL_TARGET=localhost:7233 \
  uv run harborrag doctor --json
```

The Temporal UI is at <http://localhost:8080>, and `dev.sh api` prints the API health URL
(`http://127.0.0.1:8000/api/v1/health` by default).

### 8. Ingest something

```bash
export HARBORRAG_TEMPORAL_TARGET=localhost:7233

uv run harborrag ingest start \
  --connector-id harborrag-workspace \
  --tenant tenant-1 \
  --wait
```

`--connector-id` names a connection from `config/connectors.yaml`, **not** a source type -
`--connector-id local` fails with the list of valid IDs. `--connector` is an accepted alias.

Save the run ID and drive it:

```bash
uv run harborrag ingest status RUN_ID --json
uv run harborrag ingest wait RUN_ID --json
uv run harborrag ingest watch RUN_ID      # live TUI dashboard
uv run harborrag ingest pause RUN_ID
uv run harborrag ingest resume RUN_ID
uv run harborrag ingest cancel RUN_ID
```

Re-running the same ingestion reports documents as `unchanged`. That is the admission fast
path, not an error - it skips parsing, chunking, and encoding. Use `--force-reprocess` to
override it; see [Ingestion modes](../users/ingestion-modes.md).

### 9. Query it

```bash
uv run harborrag retrieve "deployment requirements" \
  --tenant tenant-1 \
  --top-k 5 \
  --include-content
```

`retrieve` runs hybrid dense + sparse search with graph observation enabled. Use
`--lane dense|sparse|hybrid` to pin one lane and `--no-graph` to skip graph expansion.

Chat is retrieval-grounded and makes a real provider request, so it may incur charges:

```bash
uv run harborrag chat \
  "Explain HarborRAG in one paragraph." \
  --tenant tenant-1 \
  --json
```

The JSON response contains a generated session ID. Pass it back with `--session` on later
calls to keep conversation context. See the [Chat guide](../users/chat/README.md) for the
HTTP surface.

### 10. Connect an MCP client

```bash
scripts/deployment/mcp.sh --check
```

That performs a real MCP handshake and prints the four advertised retrieval tools without
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

## Something not working?

- [Troubleshooting](../users/troubleshooting/README.md) - setup, configuration, provider,
  and quality-gate failures
- [Installation](installation.md) - `pip`, editable installs, PDF/OCR backends, platform
  notes

## Continue

- [Configuration](../users/configuration/README.md) - point HarborRAG at your own sources
- [CLI Reference](../users/cli-reference/README.md) - every command and flag
- [Python SDK](../users/python-sdk/README.md) - use it as a library
- [Chat](../users/chat/README.md) - retrieval-grounded chat over HTTP and CLI
- [Architecture](../developers/architecture/README.md) - before contributing code
- [Deployment](../developers/deployment/README.md) - the full local topology and the
  production boundary
