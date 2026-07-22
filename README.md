# HarborRAG

HarborRAG is a modular, provider-agnostic Retrieval-Augmented Generation framework for engineering knowledge. It separates provider-neutral contracts, external-system adapters, RAG orchestration, runtime services, operator interfaces, and agent tools into independently testable Python packages.

> **Project status:** alpha. Connectors, parsers, model clients, repositories,
> and a repository-backed Temporal ingestion pipeline are implemented.
> The Docker Compose ingestion topology is validated for local development,
> integration testing, and controlled internal staging. Production API/MCP
> surfaces, distributed state storage, security hardening, and a public
> production topology remain incomplete.

## What is implemented

| Area | Current support |
| --- | --- |
| Connectors | Local files, GitHub, Confluence, Jira, and SharePoint |
| Parsers | Text, Markdown, JSON, CSV/TSV, HTML, EPUB, DOCX, PPTX, Excel, images, and PDF |
| PDF backends | PyMuPDF, Docling, LiteParse, MinerU, and PaddleOCR |
| Model clients | Chat, embeddings, and reranking through validated provider-neutral clients and LiteLLM-backed transports |
| Repositories | Qdrant, FalkorDB, Redis, PostgreSQL, SQLite, S3, filesystem, and in-memory implementations across vector, graph, cache, database, state, and object storage |
| Runtime | PostgreSQL-backed local Temporal deployment, durable stage state, rolling artifact fan-out, graceful worker shutdown, activities, workers, and client controls |
| Local surfaces | `doctor` and Temporal-backed ingestion commands, plus two in-process MCP test tools |

The runtime supplies a default dependency graph; deployments may override it
with a custom provider. External MCP server transport is not supplied. See
[What is HarborRAG?](docs/getting-started/what-is-harborrag.md) for the boundary.

## Requirements

- Python 3.12 or newer
- [`uv`](https://docs.astral.sh/uv/) for the recommended workspace workflow, or `pip` for editable installs
- Docker Engine with Docker Compose v2 for the local data and Temporal stacks

## Quick start

From a local checkout:

```bash
uv sync --all-packages --extra dev
uv run python -m harborrag_app.cli.main doctor --json
uv run pytest
```

The doctor command returns an `ok: true` response from the development app
service. To run the PostgreSQL-backed Temporal server, follow
[the Temporal deployment guide](deploy/temporal/README.md).

`--extra dev` is enough for the commands above, but not for the full test suite: several packages gate optional adapters (Redis, Alembic/control-plane, `pydantic-settings`, the FastAPI/JWT API surface) behind their own extras that `--extra dev` doesn't pull in. To run `uv run pytest` the way CI does, sync with every extra first:

```bash
uv sync --all-packages --all-extras
uv run pytest
```

For `pip`, platform notes, and optional adapter extras, see [Installation](docs/getting-started/installation.md).

## Run durable ingestion locally

The verified local topology runs PostgreSQL-backed Temporal, Qdrant,
FalkorDB, Redis, and two HarborRAG worker replicas. Artifact workflows use a
rolling pool with a default concurrency of 16, so a slow artifact does not
hold a completed batch slot idle. Both replicas share persistent ingestion and
model-cache volumes.

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

Do not change `TEMPORAL_POSTGRES_PASSWORD` after the PostgreSQL volume has been
initialized unless the stored PostgreSQL role password is rotated at the same
time. Changing only the environment file causes `temporal-schema` authentication
to fail.

### 2. Start data services, Temporal, and workers

Start the data stack first because it creates the external network used by the
workers:

```bash
DATABASE_ENV_FILE=env/.env.database scripts/deployment/database_up.sh
TEMPORAL_ENV_FILE=env/.env.temporal scripts/deployment/temporal_up.sh
```

The first worker build installs the selected parser/model dependencies and can
take several minutes. Downloaded Hugging Face assets persist in the shared
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

uv run --package harborrag-app harbor ingest start \
  --tenant tenant-1 \
  --connector jira \
  --wait
```

For automation, add `--json`. Save the returned run ID and use it with the
operator commands:

```bash
uv run --package harborrag-app harbor ingest status RUN_ID --json
uv run --package harborrag-app harbor ingest watch RUN_ID
uv run --package harborrag-app harbor ingest pause RUN_ID
uv run --package harborrag-app harbor ingest resume RUN_ID
uv run --package harborrag-app harbor ingest cancel RUN_ID
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
| Local development | Supported by the checked-in Compose stacks |
| Integration testing | Supported |
| Controlled single-host staging | Supported with protected ports and secrets |
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
from harborrag_adapters.connectors import ConnectorQuery, LocalFileConfig, LocalFileConnector
from harborrag_adapters.parsers import HarborParser

connector = LocalFileConnector(
    LocalFileConfig(source_path="docs", allowed_extensions={".md", ".txt", ".pdf"})
)
parser = HarborParser()

for raw_document in connector.load_raw_documents(ConnectorQuery(recursive=True)):
    parsed = parser.parse(raw_document)
    print(raw_document.source, parsed.parser_name, len(parsed.content))
```

The active connector catalog is [`config/connectors.yaml`](config/connectors.yaml).
The active parser catalog is [`config/parsers.yaml`](config/parsers.yaml), with
unused parser alternatives retained as commented blocks.

### Configure model clients

Install the model dependencies and provide the environment variables referenced by the selected file:

```bash
uv sync --all-packages --extra dev
uv run --env-file env/.env.models \
  python -m harborrag_adapters.models explain config/models.yaml --family chat
```

Model configuration resolves `${VARIABLE}` references during loading, so missing credentials fail early. The CLI can `validate`, `render`, or `explain` the chat, embedding, and reranking sections. See [Model Configuration](docs/users/configuration/model-config.md).

### Call the in-process MCP facade

```python
from harborrag_mcp.server import call_tool, list_tools

print(list_tools())
print(call_tool("harbor_health_check"))
print(call_tool("harbor_sample_retrieve", query="HarborRAG"))
```

This is an in-process test facade, not a stdio or HTTP MCP server. See [MCP Mock Tools](docs/users/detailed-guides/mcp-server/README.md).

## Workspace packages

```text
packages/
  harborrag-core/      domain objects, model contracts, schemas, security
  harborrag-adapters/  connectors, parsers, model clients, repositories
  harborrag-engine/    ingestion, retrieval, indexing, graph boundaries
  harborrag-runtime/   production composition and Temporal orchestration
  harborrag-app/       application service, CLI, HTTP API boundary
  harborrag-mcp/       MCP tools/server boundary, policy, audit
  harborrag/           thin public facade / meta-package
  harborrag-memory/    reserved placeholder; not a uv workspace member
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
- [Contributing](CONTRIBUTING.md)
- [Changelog](CHANGELOG.md)

## License

Licensed under the terms in [LICENSE](LICENSE).
