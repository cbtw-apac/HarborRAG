<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="website/assets/logos/HarborRAG-logo-dark.png">
    <source media="(prefers-color-scheme: light)" srcset="website/assets/logos/HarborRAG-logo-light.png">
    <img src="website/assets/logos/HarborRAG-logo-light.png" alt="HarborRAG" width="360">
  </picture>
</p>

<h3 align="center">A modular, provider-agnostic RAG framework for engineering knowledge</h3>

<p align="center">
  Ingest from the systems your team already uses, and retrieve answers that can only cite
  the document version that is actually current.
</p>

<p align="center">
  <a href="https://www.python.org/downloads/"><img alt="Python 3.12+" src="https://img.shields.io/badge/python-3.12%2B-blue"></a>
  <a href="https://cbtw-apac.github.io/HarborRAG/coverage/"><img alt="Test coverage" src="https://img.shields.io/badge/coverage-view%20reports-blue"></a>
  <a href="https://www.apache.org/licenses/LICENSE-2.0"><img alt="License: Apache 2.0" src="https://img.shields.io/badge/License-Apache%202.0-blue.svg"></a>
</p>

---

> **Release Note** `2.0.0a1` is the new public release. It is built for local
> development, integration testing, and controlled single-host staging. A hardened
> multi-tenant production topology is not supplied yet - see
> [Where HarborRAG is production-ready](#where-harborrag-is-production-ready).

## Why HarborRAG

### Reach the knowledge that is actually scattered

Engineering context does not live in one place. HarborRAG ingests from **local files,
GitHub, Confluence, Jira, and SharePoint** — including issue comments, changelogs, and
attachments, not just page bodies — and reads **eight parser families** covering text,
Markdown, HTML, JSON, CSV/Excel, DOCX, ODT, EPUB, PPTX, images, and PDF. PDFs route through
your choice of PyMuPDF, Docling, LiteParse, MinerU, or PaddleOCR, so a scanned spec and a
born-digital one both land as text.

### Answers cannot cite a stale version

Most RAG stacks answer from whatever happens to be in the vector index. If a document was
updated an hour ago and the reindex is still running, the old text is still retrievable —
and still citable.

HarborRAG treats the vector and graph stores as **rebuildable projections**, never the
source of truth:

- **PostgreSQL** holds the authoritative document, version, and active-publication state.
- **Object storage** holds immutable evidence — the exact bytes each answer cites.
- **Qdrant and FalkorDB** hold search accelerators that can be dropped and rebuilt.

A version becomes retrievable only after its projections verify, and every retrieval
candidate is checked against the active version in PostgreSQL before its evidence is
resolved. A superseded version cannot be cited, even mid-reindex.

### Re-ingesting doesn't re-pay the parse and embedding bill

Parsing a 400-page PDF and embedding its chunks costs real time and real provider spend.
Incremental admission compares source descriptors, processing profile, canonical content,
and retrieval metadata, and reports an untouched document as `unchanged` — **skipping
parsing, chunking, and encoding entirely.** Re-running an ingestion over a mostly-static
Confluence space is close to free.

### Rebuild the index without re-crawling your sources

Because canonical content and chunks are immutable artifacts in object storage, the
`harborrag.reindex` workflow rebuilds vector and graph projections **without calling a
single connector**. Swapping an embedding model, recovering a corrupted collection, or
adding a sparse lane does not re-crawl Jira, does not burn source API quota, and does not
require those systems to be reachable at all — source identity is recovered from the stored
canonical document.

> Today this is driven programmatically through the runtime's Temporal client
> (`start_reindex`). There is no `harborrag reindex` CLI command or REST route yet; the
> admin API reports `reindex_required` and can drop a tenant's projections
> (`/v1/admin/projections/{tenant}`).

### Graph, not just vectors

Similarity search alone cannot answer "which service owns this release policy?" HarborRAG
projects a **FalkorDB knowledge graph** alongside the vector index, carrying deterministic
document structure plus source-declared relationships. Retrieval runs dense, sparse, or
hybrid lanes with optional graph observation, and agents get three graph tools to walk from
a retrieved chunk to triplets, bounded paths, and neighbourhoods — every hop resolved
against the *active* version, so graph traversal inherits the same freshness guarantee.

### Long ingestion runs survive contact with reality

Ingestion is orchestrated durably by Temporal. A run survives worker restarts, retries a
single failed document rather than the whole job, and can be paused, resumed, or cancelled
at safe source-batch boundaries. Temporal is deliberately kept **off** the
latency-sensitive retrieval path.

### Nothing is hardwired

Connectors, parsers, model providers, vector stores, graph stores, and caches all sit
behind provider-neutral contracts across eight independently testable packages. Choosing
Qdrant or OpenAI is a line of configuration, not an architectural commitment.

<details markdown="1">
<summary><strong>How the guarantee actually works</strong></summary>

HarborRAG separates authority, evidence, and search acceleration instead of asking one
database to play all three roles:

| Boundary | Responsibility |
| --- | --- |
| PostgreSQL | Authoritative tenant, source, document, version, job, and active-publication state |
| S3-compatible object store | Immutable raw, parsed, canonical, chunk, representation, and manifest artifacts |
| Qdrant | Rebuildable dense and sparse vector projections |
| FalkorDB | Rebuildable deterministic structure and source-declared relationship projections |

Temporal coordinates durable ingestion and replay. It is deliberately **not** on the
latency-sensitive retrieval path.

Read the [data lifecycle](docs/developers/architecture/data-lifecycle.md) for the complete
path, [projection rebuild](docs/developers/architecture/projection-rebuild.md) for how
accelerators are regenerated without re-fetching sources, and
[runtime reliability](docs/developers/architecture/runtime-reliability.md) for workflow,
retry, and failure behavior.

</details>

## 5-minute tour

No credentials, no Docker, and no backing services. You need Python 3.12+ and
[`uv`](https://docs.astral.sh/uv/). The workspace sync needs roughly 520 MB of disk.

```bash
git clone https://github.com/cbtw-apac/HarborRAG.git
cd HarborRAG
uv sync --all-packages --extra dev
```

Look at the operator CLI:

```bash
uv run harborrag --help
```

Parse a real document through the real parser registry - it picks a parser family, then
routes to a concrete engine:

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

Run it from the checkout root - `source_uri` is repository-relative. It prints the parser
family, the engine that handled the document, and the extracted character count, for
example `markup markdown 663`.

That is parsing on its own. Ingestion and retrieval need backing services, which is what
[Run the full stack locally](#run-the-full-stack-locally) sets up.

## Install

`harborrag` is the only package you install. It pulls in the rest of the workspace, and
extras add the providers you actually use:

```bash
pip install "harborrag[all]"          # everything
pip install "harborrag[cli,qdrant]"   # or just what you need
```

A bare `pip install harborrag` gives you the contracts and no provider clients - no vector
store, no graph store, no model client - so add at least one extra.

| Extra | Install it when you want |
| --- | --- |
| `local` | local end-to-end ingestion and retrieval |
| `chat` | chat completion, embeddings, or reranking |
| `cli` | the `harborrag` command |
| `server` | the HTTP control-plane API |
| `mcp` | MCP retrieval tools in an IDE or agent |
| `temporal` | durable ingestion (`submit`/`pause`/`resume`/`cancel`) |
| `all` | the full surface |

Provider-specific extras exist too: `memory`, `qdrant`, `falkordb`, `postgres`, `s3`,
`redis`. See [Installation](docs/getting-started/installation.md) for what each one pulls
in, `pip` and editable installs, PDF/OCR backends, and platform notes.

> **Contributing or running the test suite?** Use the checkout above instead of `pip`. It
> ships the example configuration, the deployment scripts, and the tests, which the PyPI
> packages do not.

## Use it from Python

HarborRAG is a library first. `HarborRAG` exposes four async service facades -
`ingestion`, `retrieval`, `graph`, and `chat` - behind one async context manager:

```python
import asyncio

from harborrag import AccessContext, HarborRAG, IngestionRequest, RetrievalRequest


async def main() -> None:
    access = AccessContext(principal_id="user-1", tenant_id="tenant-1")

    async with HarborRAG.from_config("config/harborrag.example.yaml") as harbor:
        await harbor.ingestion.run(
            IngestionRequest(access=access, connector_name="harborrag-workspace")
        )
        results = await harbor.retrieval.search(
            RetrievalRequest(access=access, query="deployment requirements")
        )
        print(results)


asyncio.run(main())
```

Connectors are declared once in [`config/connectors.yaml`](config/connectors.yaml) and
selected by name, so credentials stay as environment references rather than arguments.

Long runs can be driven durably instead of inline:

```python
task = await harbor.ingestion.submit(request)   # returns a task reference
status = await harbor.ingestion.status(task.task_id)
await harbor.ingestion.pause(task.task_id)
await harbor.ingestion.resume(task.task_id)
await harbor.ingestion.cancel(task.task_id)
```

All five durable controls - `submit`, `status`, `pause`, `resume`, `cancel` - need
`execution_mode: temporal` and the `harborrag[temporal]` extra, and raise
`ExecutionCapabilityError` without both. `run` executes directly and needs neither. Either
path needs the services from
[Run the full stack locally](#run-the-full-stack-locally). More examples:
[Python SDK](docs/users/python-sdk/README.md).

## Set up the `env/` folder

HarborRAG keeps every credential out of the repository. Configuration lives in tracked
[`config/*.yaml`](config/) files that reference `${VARIABLE}` placeholders; the values live
in an ignored `env/` folder that you create once.

One command creates every file from its template, generates a random MCP bearer token, and
locks the folder down to mode `0600`:

```bash
scripts/deployment/dev.sh bootstrap
```

Then open the files it created and replace the placeholders:

| File | Holds | Needed by |
| --- | --- | --- |
| `env/.env.database` | PostgreSQL, MinIO, Qdrant, FalkorDB, and Redis credentials and ports | data stack, worker, API, MCP |
| `env/.env.temporal` | Temporal ports, namespace, and worker replica count | Temporal stack, worker |
| `env/.env.connector` | Source credentials - Jira, Confluence, GitHub, SharePoint, local paths | worker |
| `env/.env.parser` | Optional parser/OCR settings; the default profile needs none | worker |
| `env/.env.models` | Chat, embedding, and reranking provider credentials | worker, API, MCP, CLI chat |
| `env/.env.api` | API bind address, port, and JWT settings | API, MCP |
| `env/.env.mcp` | Local MCP bearer token and transport overrides | MCP HTTP transport |

### Three values you must fill in

`bootstrap` leaves working local defaults everywhere except these. The first one is
mandatory - Compose refuses to start without it:

```bash
# 1. env/.env.database - ships EMPTY and blocks startup.
#    Encrypts stored connector credentials in the control database. The API and the
#    worker must share the same value, and it cannot change once secrets are stored.
openssl rand -hex 32
```

```dotenv
# env/.env.database
HARBORRAG_SECRETS_ENCRYPTION_KEY=<paste the value above>
POSTGRES_PASSWORD=<a strong password, replacing change-me>
```

```dotenv
# env/.env.models - the active config/models.yaml needs all six.
HARBOR_CHAT_PROVIDER=openai
HARBOR_CHAT_MODEL=gpt-4o-mini
HARBOR_CHAT_API_KEY=<your key>
HARBOR_EMBED_PROVIDER=openai
HARBOR_EMBED_MODEL=text-embedding-3-small
HARBOR_EMBED_API_KEY=<your key>
```

Model references are expanded eagerly at load time, so a missing *embedding* variable
fails just as hard as a missing chat one.

> **If `dev.sh up` dies with**
> `required variable HARBORRAG_SECRETS_ENCRYPTION_KEY is missing a value` - that is this
> step. Compose treats an empty value the same as an unset one.

<details markdown="1">
<summary><strong>Details: what reads these files, and how to point elsewhere</strong></summary>

**Catalog loaders never read `env/` on their own.** Export the variables in your shell,
pass a file to `uv run --env-file`, or load them through your application or secret
manager. The checked-in Compose worker explicitly loads `env/.env.connector`,
`env/.env.parser`, and `env/.env.models`; the API and MCP launchers load the files listed
in the table above.

**Overriding paths.** Both scripts read the same environment-file variables, so you can
keep secrets outside the checkout:

```bash
DATABASE_ENV_FILE=/etc/harborrag/database.env \
MODEL_ENV_FILE=/etc/harborrag/models.env \
  scripts/deployment/dev.sh data
```

`dev.sh` accepts `DATABASE_ENV_FILE`, `TEMPORAL_ENV_FILE`, `CONNECTOR_ENV_FILE`,
`PARSER_ENV_FILE`, `MODEL_ENV_FILE`, `API_ENV_FILE`, and `MCP_ENV_FILE`. `mcp.sh` accepts
`DATABASE_ENV_FILE`, `MODEL_ENV_FILE`, `API_ENV_FILE`, and `MCP_ENV_FILE`.

**Doing it by hand.** `bootstrap` is `cp` plus `chmod` plus token generation. The manual
equivalent:

```bash
install -m 700 -d env
for name in database temporal connector parser models api mcp; do
  test -f "env/.env.${name}" || cp "env-example/.env.${name}.example" "env/.env.${name}"
done
chmod 600 env/.env.*
# HARBORRAG_SECRETS_ENCRYPTION_KEY in env/.env.database, and
# HARBORRAG_MCP_BEARER_TOKEN in env/.env.mcp (at least 32 bytes), both want:
openssl rand -hex 32
```

**Connectors.** The checked-in [`config/connectors.yaml`](config/connectors.yaml) enables
three connections: `harborrag-workspace` (local files, no credentials), `confluence-main`,
and `jira-main`. The two remote ones need credentials in `env/.env.connector` before they
will build - until then the worker logs a warning at startup, they still appear in
`GET /v1/connections`, and submitting against them fails:

```dotenv
JIRA_BASE_URL=https://your-domain.atlassian.net
JIRA_PROJECT_KEY=ENG
JIRA_EMAIL=service-account@example.com
JIRA_TOKEN=replace-with-a-least-privilege-token

CONFLUENCE_BASE_URL=https://your-domain.atlassian.net/wiki
CONFLUENCE_SPACE_KEY=ENG
CONFLUENCE_EMAIL=service-account@example.com
CONFLUENCE_TOKEN=replace-with-a-least-privilege-token
```

Set `enabled: false` on the ones you are not using if you would rather not see the
warning. `harborrag-workspace` alone is enough to complete the quick start.

**Monitoring.** The optional Prometheus/Grafana/Loki stack under [`deploy/`](deploy/) is
the eighth template, and `bootstrap` does **not** create it. It needs three things:

```bash
cp env-example/.env.monitoring.example env/.env.monitoring
chmod 600 env/.env.monitoring
# then set a non-empty GRAFANA_ADMIN_PASSWORD -- it ships empty and is guarded like
# HARBORRAG_SECRETS_ENCRYPTION_KEY, and run `dev.sh data` first so the shared
# harborrag-data-network exists.
```

Note that Grafana and the FalkorDB browser both default to host port `3000`, so whichever
starts second fails to bind. Change `GRAFANA_PORT` or `FALKORDB_BROWSER_PORT`.

See [Configuration](docs/users/configuration/README.md) for the full reference.

</details>

## Run the full stack locally

The local topology runs PostgreSQL-backed Temporal, Qdrant, FalkorDB, Redis, MinIO, the
HarborRAG ingestion workers, and the control-plane API. One command starts all of it:

```bash
scripts/deployment/dev.sh up
```

On a clean checkout `up` bootstraps the `env/` folder and stops so you can review the
credentials. Run it again once you have replaced the placeholders.

The first worker build installs the selected parser and model dependencies and can take
several minutes. Downloaded Hugging Face assets persist in the shared
`harborrag-model-cache` volume and survive container replacement.

Start pieces individually when you only need part of the stack:

| Command | Starts |
| --- | --- |
| `dev.sh data` | PostgreSQL, Qdrant, FalkorDB, Redis, MinIO - creates the shared network, so run it first |
| `dev.sh temporal` | Temporal server, schema, namespace, and UI - never a worker |
| `dev.sh worker` | the ingestion worker only |
| `dev.sh api` | the control-plane API only |
| `dev.sh up --no-worker` | everything except the worker |
| `dev.sh down` | stop everything (add `--volumes` to discard data) |

Add `--build` to `up`, `worker`, or `api` after changing source, dependencies, or baked
worker configuration. A missing image is built automatically on first start.

### Verify it came up

```bash
HARBORRAG_TEMPORAL_TARGET=localhost:7233 \
  uv run harborrag doctor --json
```

`harborrag doctor` talks to a live Temporal frontend, so run it only after
`dev.sh temporal` succeeds. The Temporal UI is at <http://localhost:8080>, and the API
health endpoint is printed by `dev.sh api`.

<details markdown="1">
<summary><strong>Details: container inspection and log following</strong></summary>

```bash
docker compose \
  --env-file env/.env.database \
  --file deploy/compose/docker-compose.database.yml \
  ps

docker compose \
  --env-file env/.env.database \
  --env-file env/.env.temporal \
  --file deploy/compose/docker-compose.temporal.yml \
  --profile worker \
  ps -a
```

`postgresql`, `temporal`, `temporal-ui`, and every `temporal-worker` replica should be
running. `temporal-schema` and `temporal-namespace` are one-shot jobs and should show
`Exited (0)`.

Follow worker logs during a run:

```bash
docker compose \
  --env-file env/.env.database \
  --env-file env/.env.temporal \
  --file deploy/compose/docker-compose.temporal.yml \
  --profile worker \
  logs --follow --tail=200 temporal-worker
```

**Container base image.** All four Dockerfiles use
`ghcr.io/astral-sh/uv:python3.12-bookworm-slim`. It must stay a glibc (Debian) image: the
locked `temporalio`, PyTorch, TorchVision, and ONNX Runtime releases publish manylinux
wheels only, so the musl (alpine) variants cannot be used. uv ships in the base image, so
nothing pip-installs it. `tests/test_release_images_use_lock.py` enforces the shared base
and the frozen-lock install path. The tag floats, so uv and Python patch versions can move
between builds; application dependencies stay reproducible through `uv export --frozen`
against `uv.lock`. These images are not built in CI.

**Capacity is deployment policy.** Concurrency, retry budgets, and worker counts belong to
your deployment. Tune them for source quotas, model latency, document size, and available
resources rather than treating the repository defaults as universal. Worker capacity, task
queues, retry budgets, TLS, and timeouts live in
[`config/temporal.yaml`](config/temporal.yaml); the replica count lives in
`env/.env.temporal` as `HARBORRAG_TEMPORAL_WORKER_REPLICAS`.

</details>

### Run an ingestion

```bash
export HARBORRAG_TEMPORAL_TARGET=localhost:7233

uv run harborrag ingest start \
  --connector-id harborrag-workspace \
  --tenant tenant-1 \
  --wait
```

`--connector-id` names a connector defined in
[`config/connectors.yaml`](config/connectors.yaml) - the checked-in catalog defines
`harborrag-workspace`, `confluence-main`, and `jira-main`. It is not a source *type*, so
`--connector-id local` fails with the list of valid IDs.

Save the returned run ID - add `--json` for machine-readable output - and drive it with the
operator commands:

```bash
uv run harborrag ingest status RUN_ID --json
uv run harborrag ingest wait RUN_ID --json
uv run harborrag ingest watch RUN_ID      # live TUI dashboard
uv run harborrag ingest pause RUN_ID
uv run harborrag ingest resume RUN_ID
uv run harborrag ingest cancel RUN_ID
```

Cancellation is applied at a safe source-batch boundary. A later run may report documents
as `unchanged` - that is the expected admission fast path, and it skips parsing, chunking,
and encoding entirely.

### Query it

```bash
uv run harborrag retrieve "deployment requirements" \
  --tenant tenant-1 \
  --top-k 5 \
  --include-content

uv run harborrag chat "How do we deploy the worker?" \
  --tenant tenant-1 \
  --json
```

`retrieve` runs hybrid dense + sparse search with graph observation on by default
(`--lane dense|sparse|hybrid`, `--no-graph` to disable). `chat` makes a real provider
request against `config/models.yaml`, so it may incur provider charges; its JSON response
carries a session ID you can pass back with `--session` to keep conversation context.

More: [CLI reference](docs/users/cli-reference/README.md) ·
[Ingestion modes](docs/users/ingestion-modes.md) ·
[Deployment guide](docs/developers/deployment/README.md)

## Connect an MCP client

HarborRAG exposes **four** audited, tenant-scoped retrieval tools over MCP:
`vector_search`, `graph_triplet_search`, `graph_path_search`, and
`graph_subgraph_search`. Every call requires an explicit tenant, passes JSON-schema and
budget validation, and is recorded in an owner-only audit log that stores argument digests
rather than raw query text.

Chat and agent operations are **not** MCP tools - they are served through the REST API.
Ingestion is controlled through the CLI or the authenticated API.

Check the installation and list the advertised tools:

```bash
scripts/deployment/mcp.sh --check
```

For normal use, point your MCP client at `scripts/deployment/mcp.sh`. The stdio server is
not an interactive terminal or an HTTP service and opens no network listener, so launch it
from a client with stdin and stdout connected to pipes.

For a local authenticated HTTP endpoint plus a browser status page:

```bash
scripts/deployment/dev.sh bootstrap
scripts/deployment/mcp.sh --http
```

Open <http://127.0.0.1:8010/> and clients connect to `http://127.0.0.1:8010/mcp` with the
bearer token from `env/.env.mcp`. The page includes an owner-only Tool Playground and an
editor for [`config/mcp.yaml`](config/mcp.yaml): enter the token, load the effective tools
for a tenant, fill in the generated argument form, and run retrieval without a separate MCP
client. Playground calls use the same schema, policy, tenant configuration, and audit path
as MCP calls.

The launcher binds loopback only and rejects anything else. Remote exposure requires TLS
and a production JWT/JWKS verifier. See
[MCP Tools](docs/users/detailed-guides/mcp-server/README.md) and
[MCP Setup and Integration](docs/users/detailed-guides/mcp-server/setup-and-integration.md).

## What is implemented

| Area | Current support |
| --- | --- |
| Connectors | Local files, GitHub, Confluence, Jira, SharePoint |
| Parsers | Text, Markdown, JSON, CSV/TSV, HTML, EPUB, DOCX, PPTX, Excel, images, PDF |
| PDF backends | PyMuPDF, Docling, LiteParse, MinerU, PaddleOCR |
| Model clients | Chat, embeddings, and reranking through provider-neutral clients and LiteLLM-backed transports |
| Repositories | Qdrant, FalkorDB, Redis, PostgreSQL, SQLite, S3, filesystem, and in-memory implementations across vector, graph, cache, database, state, and object storage |
| Runtime | PostgreSQL-backed local Temporal deployment, durable stage state, rolling artifact fan-out, graceful worker shutdown, activities, workers, client controls |
| Operator surfaces | FastAPI ingestion control, Temporal-backed CLI, hybrid graph/vector retrieval, and an authenticated MCP boundary |

The runtime ships a default dependency graph; a deployment may substitute its own provider.
[What is HarborRAG?](docs/getting-started/what-is-harborrag.md) records the precise
implemented/scaffolded boundary.

### Where HarborRAG is production-ready

| Target | Status |
| --- | --- |
| Local development | Supported - host uv workflow and containerized worker |
| Integration testing | Supported |
| Controlled single-host staging | Supported once the manual ingestion release gate passes against the deployed candidate |
| Public or multi-tenant production | **Not yet supplied as a complete topology** |

Before a public launch you still own: Temporal Cloud or a hardened self-hosted Temporal,
TLS and network policy, secret-manager integration, API authentication and authorization,
ACL projection, multi-tenancy, production observability, and tested backup/restore.
[Deployment](docs/developers/deployment/README.md) has the complete readiness boundary, and
the [deployed ingestion smoke guide](packages/harborrag-runtime/tests/runtime_ingestion/smoke/README.md)
documents the release gate that validates the Postgres control plane, immutable MinIO
artifacts, Qdrant and FalkorDB projections, Redis-loss behavior, Temporal workflows,
authoritative retrieval, and connector-free reindexing.

## The eight packages

```text
packages/
  harborrag-core/       domain objects, model contracts, schemas, security
  harborrag-adapters/   connectors, parsers, model clients, repositories
  harborrag-memory/     scope-aware short-term, working, and long-term memory
  harborrag-engine/     ingestion, retrieval, indexing, graph boundaries
  harborrag-runtime/    production composition and Temporal orchestration
  harborrag-app/        application service, CLI, HTTP API boundary
  harborrag-mcp-server/ MCP tools and server boundary, policy, audit
  harborrag/            thin public facade / meta-package
```

Dependencies only ever point one way, and
`scripts/check_dependency_direction.py` enforces it:

```text
core
  ├─ adapters
  └─ memory
       └─ engine
            └─ runtime
                 ├─ app
                 └─ mcp
```

`harborrag` may re-export stable APIs from any implemented package. See
[Architecture](docs/developers/architecture/README.md) for the exact allowed-import table
and ownership rules.

## Configuration files

| File | Purpose |
| --- | --- |
| [`config/connectors.yaml`](config/connectors.yaml) | Active named connector definitions and environment references |
| [`config/parsers.yaml`](config/parsers.yaml) | Active parser definitions, with backend alternatives kept as commented blocks |
| [`config/models.yaml`](config/models.yaml) | Active chat and embedding model configuration |
| [`config/temporal.yaml`](config/temporal.yaml) | Temporal connection, TLS, task queues, retries, worker capacity, health, workflow timeouts |
| [`config/mcp.yaml`](config/mcp.yaml) | MCP tool enablement, defaults, limits, and tenant overrides |

Annotated references and richer examples live alongside them:
`config/temporal.example.yaml`, `config/models.advance.example.yaml`,
`config/harborrag.example.yaml`, and `config/advance_chat/*.example.yaml`. Templates for
every environment file live in [`env-example/`](env-example/).

Model configuration resolves `${VARIABLE}` references while loading, so a missing
credential fails immediately rather than at first use. Inspect what a catalog resolves to
before running anything real:

```bash
uv run --env-file env/.env.models \
  python -m harborrag_adapters.models explain config/models.yaml --family chat
```

The same CLI can `validate` and `render` the chat, embedding, and reranking sections. See
[Model configuration](docs/users/configuration/model-config.md).

## Documentation

| Start here | |
| --- | --- |
| [What is HarborRAG?](docs/getting-started/what-is-harborrag.md) | Concepts and the implemented/scaffolded boundary |
| [Installation](docs/getting-started/installation.md) | Every install path, extra, and platform note |
| [Quick start](docs/getting-started/quick-start.md) | Checkout to first ingested document |
| [Documentation index](docs/TOC.md) | Everything, organized |

| Using it | |
| --- | --- |
| [Configuration](docs/users/configuration/README.md) | Connectors, parsers, models, engine, tenants |
| [CLI reference](docs/users/cli-reference/README.md) | Every operator command |
| [Python SDK](docs/users/python-sdk/README.md) | Library usage |
| [Chat](docs/users/chat/README.md) | Retrieval-grounded chat over HTTP and CLI |
| [MCP tools](docs/users/detailed-guides/mcp-server/README.md) | Agent and IDE integration |
| [Troubleshooting](docs/users/troubleshooting/README.md) | When something does not work |

| Building on it | |
| --- | --- |
| [Architecture](docs/developers/architecture/README.md) | Package ownership and boundaries |
| [Data lifecycle](docs/developers/architecture/data-lifecycle.md) | Authority, evidence, projections, publication |
| [Extending HarborRAG](docs/developers/extending/README.md) | New connectors, parsers, models, repositories |
| [Testing](docs/developers/testing/README.md) | Layout, markers, quality gates, smoke checks |
| [Deployment](docs/developers/deployment/README.md) | Local stacks and the production boundary |
| [Control Plane API](packages/harborrag-app/src/harborrag_app/api/README.md) | HTTP surface |

## Contributing

Contributions are welcome. [CONTRIBUTING.md](CONTRIBUTING.md) covers setup, package
ownership, review expectations, and the quality gates.

The full local gate mirrors CI:

```bash
uv sync --all-packages --all-extras
uv run make lint
uv run make typecheck
uv run make deps-check
uv run make compile
uv run make coverage
```

`make coverage` enforces the repository's 90% threshold. Default tests are hermetic; real
connectors, model providers, parser engines, and repository services are exercised only
through opt-in smoke scripts. Run `uv run make help` for every target.

Maintainers: the publication sequence lives in
[Release process](docs/developers/release-process.md).

## Community and support

- **Questions and ideas** - [GitHub Discussions](https://github.com/cbtw-apac/HarborRAG/discussions)
- **Bugs and feature requests** - [GitHub Issues](https://github.com/cbtw-apac/HarborRAG/issues)
- **Security** - do not open a public issue; follow [SECURITY.md](SECURITY.md)

<details markdown="1">
<summary><strong>Coming from Qdrant Loader?</strong></summary>

HarborRAG 2.0 is Qdrant Loader, renamed and restructured from a single Qdrant-coupled
ingestion tool into a provider-agnostic framework. Qdrant is now one vector adapter among
several rather than the hardwired store.

This is a **breaking** change. Import paths, distribution names, CLI commands, and
configuration keys from `qdrant-loader` do not carry over. The closest equivalents:

| Qdrant Loader | HarborRAG |
| --- | --- |
| `qdrant-loader` | `harborrag-app` for the CLI, or `harborrag` as the library facade |
| `qdrant-loader-core` | `harborrag-core`, `harborrag-engine`, `harborrag-memory`, `harborrag-runtime`, and `harborrag-adapters` |
| `qdrant-loader-mcp-server` | `harborrag-mcp-server` |

There is no automated migration path from a 1.x deployment: reconfigure connectors,
parsers, and models against the new catalogs in [`config/`](config/), then reingest. The
[2.0.0a1 changelog](CHANGELOG.md#200a1---2026-08-27) records the full migration boundary.

</details>

## License

HarborRAG is licensed under the [Apache License 2.0](LICENSE). Every published package
declares `Apache-2.0` and ships a copy of the license in its distribution.

<p align="center">
  <sub>A project of <strong>CBTW</strong></sub>
</p>
