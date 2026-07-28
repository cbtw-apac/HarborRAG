# HarborRAG

HarborRAG is a modular, provider-agnostic Retrieval-Augmented Generation framework for engineering knowledge. It separates provider-neutral contracts, external-system adapters, RAG orchestration, runtime services, operator interfaces, and agent tools into independently testable Python packages.

> **Project status:** alpha. Connectors, parsers, model clients, repositories,
> a repository-backed Temporal ingestion pipeline, the operator CLI, and the
> control-plane API are implemented. The checked-in data and Temporal Compose
> definitions are suitable for local development, but the current
> Alpine-based HarborRAG application images do not build with the locked
> Temporal and ML dependencies. Public production topology, distributed
> object storage, and full MCP capability exposure remain incomplete.

## What is implemented

| Area | Current support |
| --- | --- |
| Connectors | Local files, GitHub, Confluence, Jira, and SharePoint |
| Parsers | Text, Markdown, JSON, CSV/TSV, HTML, EPUB, DOCX, PPTX, Excel, images, and PDF |
| PDF backends | PyMuPDF, Docling, LiteParse, MinerU, and PaddleOCR |
| Model clients | Chat, embeddings, and reranking through validated provider-neutral clients and LiteLLM-backed transports |
| Repositories | Qdrant, FalkorDB, Redis, PostgreSQL, SQLite, S3, filesystem, and in-memory implementations across vector, graph, cache, database, state, and object storage |
| Runtime | PostgreSQL-backed local Temporal deployment, durable stage state, rolling artifact fan-out, graceful worker shutdown, activities, workers, and client controls |
| Operator surfaces | FastAPI ingestion control, Temporal-backed CLI commands, hybrid graph/vector retrieval, and an authenticated MCP server boundary with a local stdio health tool |

The runtime supplies a default dependency graph; deployments may override it
with a custom provider. MCP ingestion capabilities remain disabled until they
are explicitly implemented and enabled; the shipped stdio transport exposes
the health tool only. See [What is HarborRAG?](docs/getting-started/what-is-harborrag.md)
for the boundary.

## Requirements

- Python 3.12 or newer
- [`uv`](https://docs.astral.sh/uv/) for the recommended workspace workflow, or `pip` for editable installs
- Docker Engine with Docker Compose v2 for the local data and Temporal stacks

## Quick start

From a local checkout:

```bash
uv sync --all-packages --extra dev
uv run --package harborrag-app harborrag --help
uv run pytest packages/harborrag-core/tests
```

The CLI help command does not require external services. `harborrag doctor`
checks a live Temporal frontend, so run it only after starting Temporal. See
[the Temporal deployment guide](deploy/temporal/README.md).

`--extra dev` is enough for the commands above, but not for the full test
suite. Several packages gate optional adapters (Redis, Alembic/control-plane,
`pydantic-settings`, and the FastAPI/JWT API surface) behind their own extras.
To run `uv run pytest` the way CI does, sync with every extra first:

```bash
uv sync --all-packages --all-extras
uv run pytest
```

For `pip`, platform notes, and optional adapter extras, see [Installation](docs/getting-started/installation.md).

## Configure durable ingestion locally

The intended local topology runs PostgreSQL-backed Temporal, Qdrant,
FalkorDB, Redis, and two HarborRAG worker replicas. Artifact workflows use a
rolling pool with a default concurrency of 16, so a slow artifact does not
hold a completed batch slot idle. Both replicas share persistent ingestion and
model-cache volumes.

> **Container base image:** all four Dockerfiles use
> `ghcr.io/astral-sh/uv:python3.12-bookworm-slim`. It must stay a glibc (Debian)
> image: the locked `temporalio`, PyTorch, TorchVision, and ONNX Runtime
> releases publish manylinux wheels only, so the musl (alpine) variants cannot
> be used. uv ships in the base image, so nothing pip-installs it.
> `tests/test_release_images_use_lock.py` enforces the shared base and the
> frozen-lock install path. The tag floats, so the uv and Python patch versions
> can move between builds; the application dependencies themselves stay
> reproducible via `uv export --frozen` against `uv.lock`. Note that the images
> are not built in CI.

### 1. Create protected environment files

```bash
install -m 700 -d env

test -f env/.env.database || \
  cp env-example/.env.database.example env/.env.database
test -f env/.env.temporal || \
  cp env-example/.env.temporal.example env/.env.temporal
test -f env/.env.connector || \
  cp env-example/.env.connector.example env/.env.connector
test -f env/.env.parser || \
  cp env-example/.env.parser.example env/.env.parser
test -f env/.env.models || \
  cp env-example/.env.models.example env/.env.models

chmod 600 env/.env.*
```

Replace every placeholder credential. The enabled Jira connector in
[`config/connectors.yaml`](config/connectors.yaml) requires these values in
`env/.env.connector`:

```dotenv
JIRA_BASE_URL=https://your-domain.atlassian.net
JIRA_PROJECT_KEY=ENG
JIRA_EMAIL=service-account@example.com
JIRA_TOKEN=replace-with-a-least-privilege-token
```

The active model catalog in [`config/models.yaml`](config/models.yaml) requires
the `HARBOR_CHAT_*` and `HARBOR_EMBED_*` values from `env/.env.models`. Set a
strong `POSTGRES_PASSWORD` in `env/.env.database`, and configure the Temporal
worker in `env/.env.temporal`:

```dotenv
TEMPORAL_POSTGRES_PASSWORD=replace-with-a-long-random-password
TEMPORAL_START_WORKER=1
HARBORRAG_TEMPORAL_WORKER_REPLICAS=2
```

`temporal_up.sh` keeps the stored PostgreSQL role password synchronized with
`TEMPORAL_POSTGRES_PASSWORD` when the existing development volume is reused.
Use the script after changing the password; invoking Compose directly does not
perform that synchronization.

### 2. Start data services, Temporal, and workers

Start the data stack first because it creates the external network used by the
workers:

```bash
DATABASE_ENV_FILE=env/.env.database bash scripts/deployment/database_up.sh
TEMPORAL_ENV_FILE=env/.env.temporal bash scripts/deployment/temporal_up.sh
```

After the application image compatibility issue above is resolved, the first
worker build installs the selected parser/model dependencies and can take
several minutes. Downloaded Hugging Face assets persist in the shared
`harborrag-model-cache` volume and are reused after container replacement.

### 3. Verify the deployment

```bash
docker compose \
  --env-file env/.env.database \
  --file deploy/compose/docker-compose.database.yml \
  ps

docker compose \
  --env-file env/.env.temporal \
  --file deploy/compose/docker-compose.temporal.yml \
  --profile worker \
  ps -a

HARBORRAG_TEMPORAL_TARGET=localhost:7233 \
  uv run python -m harborrag_app.cli.main doctor --json
```

`postgresql`, `temporal`, `temporal-ui`, and both `temporal-worker` replicas
should be running. `temporal-schema` and `temporal-namespace` are successful
one-shot jobs and should show `Exited (0)`. The Temporal UI is available at
<http://localhost:8080> for local use.

### 4. Submit and observe Jira ingestion

```bash
export HARBORRAG_TEMPORAL_TARGET=localhost:7233

uv run --package harborrag-app harborrag ingest start \
  --tenant tenant-1 \
  --connector jira \
  --wait
```

For automation, add `--json`. Save the returned run ID and use it with the
operator commands:

```bash
uv run --package harborrag-app harborrag ingest status RUN_ID --json
uv run --package harborrag-app harborrag ingest watch RUN_ID
uv run --package harborrag-app harborrag ingest pause RUN_ID
uv run --package harborrag-app harborrag ingest resume RUN_ID
uv run --package harborrag-app harborrag ingest cancel RUN_ID
```

Cancellation is graceful unless `--force` is supplied. A later ingestion may
report artifacts as `unchanged`; this is the expected revision-based fast path.

Follow worker logs during a run:

```bash
docker compose \
  --env-file env/.env.temporal \
  --file deploy/compose/docker-compose.temporal.yml \
  --profile worker \
  logs --follow --tail=200 temporal-worker
```

See the [Temporal deployment guide](deploy/temporal/README.md) and
[CLI reference](docs/users/cli-reference/README.md) for persistence, worker
controls, and troubleshooting details.

## Deployment boundary

| Target | Status |
| --- | --- |
| Local development | Host-based uv workflow supported; data Compose definitions valid; Alpine application images currently blocked |
| Integration testing | Supported |
| Controlled single-host staging | Blocked until immutable application images build and pass ingestion smoke tests |
| Public or multi-tenant production | Not yet supplied as a complete topology |

Before a public production launch, replace local SQLite/filesystem ingestion
state with concurrency-safe managed storage, use Temporal Cloud or a hardened
self-hosted Temporal deployment, build immutable worker images, add TLS and
network policies, integrate a secret manager, implement API authentication and
authorization, wire production observability, and test backup/restore. See
[Deployment](docs/developers/deployment/README.md) for the complete readiness
boundary.

## Try the implemented adapters

### Load and parse local documents

```python
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
```

The active connector catalog is [`config/connectors.yaml`](config/connectors.yaml).
The active parser catalog is [`config/parsers.yaml`](config/parsers.yaml), with
unused parser alternatives retained as commented blocks. The root registry
selects a parser family; each family owns its engine routing, quality checks,
fallback behavior, and output normalization.

### Configure model clients

Install the model dependencies and provide the environment variables referenced by the selected file:

```bash
uv sync --all-packages --extra dev
uv run --env-file env/.env.models \
  python -m harborrag_adapters.models explain config/models.yaml --family chat
```

Model configuration resolves `${VARIABLE}` references during loading, so missing credentials fail early. The CLI can `validate`, `render`, or `explain` the chat, embedding, and reranking sections. See [Model Configuration](docs/users/configuration/model-config.md).

### Run the local MCP stdio transport

```bash
uv run --package harborrag-mcp-server \
  python -c \
  "from harborrag_mcp_server import create_mcp_server; create_mcp_server(allow_unauthenticated_local=True).run(transport='stdio')"
```

The unauthenticated override is restricted to local stdio and opens no network
listener. Network transports require an authentication provider. See
[MCP Tools](docs/users/detailed-guides/mcp-server/README.md).

## Workspace packages

```text
packages/
  harborrag-core/      domain objects, model contracts, schemas, security
  harborrag-adapters/  connectors, parsers, model clients, repositories
  harborrag-engine/    ingestion, retrieval, indexing, graph boundaries
  harborrag-runtime/   production composition and Temporal orchestration
  harborrag-app/       application service, CLI, HTTP API boundary
  harborrag-mcp-server/ MCP tools/server boundary, policy, audit
  harborrag/           thin public facade / meta-package
```

Dependency direction is enforced by `scripts/check_dependency_direction.py`:

```text
core
  └─ adapters
       └─ engine
            └─ runtime
                 ├─ app
                 └─ mcp

harborrag may re-export stable APIs from the implemented packages.
```

See [Architecture](docs/developers/architecture/README.md) for the exact allowed-import table and ownership rules.

## Configuration files

| File | Purpose |
| --- | --- |
| `config/connectors.yaml` | Active named connector definitions and environment references |
| `config/parsers.yaml` | Active parser definitions and commented backend alternatives |
| `config/models.yaml` | Active chat and embedding model configuration |
| `config/models.advance.example.yaml` | More advanced routing and provider examples |
| `config/advance_chat/*.example.yaml` | Direct SDK, LiteLLM Router, proxy, and distributed chat examples |
| `env-example/.env.connector.example` | Connector and connector-smoke environment template |
| `env-example/.env.parser.example` | Optional parser/OCR environment template |
| `env-example/.env.models.example` | Model and model-smoke environment template |
| `env-example/.env.database.example` | Local repository-stack template |
| `env-example/.env.temporal.example` | Local PostgreSQL/Temporal-stack template |

Catalog loaders do not automatically load environment files. Export variables
in the shell, pass an environment file to `uv run`, or load them through your
application or secret manager. The checked-in Compose worker explicitly loads
`env/.env.connector`, `env/.env.parser`, and `env/.env.models`.

## Release checklist

Release only from a reviewed, clean commit. The full local gate mirrors the
repository CI:

```bash
uv sync --all-packages --all-extras
uv run make lint
uv run make typecheck
uv run make deps-check
uv run make compile
uv run make coverage
```

After merging to `main` and confirming GitHub Actions are green, simulate the
coordinated package release before allowing it to write commits, tags, and
GitHub releases:

```bash
git switch main
git pull --ff-only
git status --short
uv run python release.py --dry-run --verbose
uv run python release.py --verbose
```

The real release command requires a clean `main`, synchronized package
versions, an updated changelog, no unpushed commits, passing workflows, and a
`GITHUB_TOKEN` authorized for this repository. See [Contributing](CONTRIBUTING.md)
for the pull-request and release gates.

## Development commands

When using the uv environment without activating it, run Make targets through `uv run`:

```bash
uv run make help
uv run make test
uv run make test-package PACKAGE=harborrag-adapters
uv run make coverage
uv run make lint
uv run make format
uv run make typecheck
uv run make compile
uv run make deps-check
uv run make doctor
```

`make coverage` enforces the repository's 90% coverage threshold. If your virtual environment is already active, the `uv run` prefix is optional. Default tests are hermetic; real connectors, model providers, parser engines, and repository services are exercised only through opt-in smoke scripts.

## Documentation

- [Documentation index](docs/TOC.md)
- [Getting started](docs/getting-started/README.md)
- [User guides](docs/users/README.md)
- [Developer guides](docs/developers/README.md)
- [Control Plane API](packages/harborrag-app/src/harborrag_app/api/README.md)
- [Operator CLI](packages/harborrag-app/src/harborrag_app/cli/README.md)
- [Contributing](CONTRIBUTING.md)
- [Changelog](CHANGELOG.md)

## License

Licensed under the terms in [LICENSE](LICENSE).
