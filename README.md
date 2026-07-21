# HarborRAG

HarborRAG is a modular, provider-agnostic Retrieval-Augmented Generation framework for engineering knowledge. It separates provider-neutral contracts, external-system adapters, RAG orchestration, runtime services, operator interfaces, and agent tools into independently testable Python packages.

> **Project status:** alpha. Connectors, document parsers, model clients, and tenant-aware repository adapters are implemented and extensively tested. The default composition, CLI, HTTP API, MCP transport, durable workflows, and public facade are still limited or scaffolded. Use the local mock path to evaluate package wiring; do not treat the repository as a finished application deployment.

## What is implemented

| Area | Current support |
| --- | --- |
| Connectors | Local files, GitHub, Confluence, Jira, and SharePoint |
| Parsers | Text, Markdown, JSON, CSV/TSV, HTML, EPUB, DOCX, PPTX, Excel, images, and PDF |
| PDF backends | PyMuPDF, Docling, LiteParse, MinerU, and PaddleOCR |
| Model clients | Chat, embeddings, and reranking through validated provider-neutral clients and LiteLLM-backed transports |
| Repositories | Qdrant, FalkorDB, Redis, PostgreSQL, SQLite, S3, filesystem, and in-memory implementations across vector, graph, cache, database, state, and object storage |
| Local surfaces | A deterministic mock pipeline, `doctor` and `sample-ingest` CLI commands, and two in-process MCP mock tools |

The production ingestion/retrieval composition, FastAPI routes, external MCP server transport, and Temporal integration are not complete. See [What is HarborRAG?](docs/getting-started/what-is-harborrag.md) for the capability boundary.

## Requirements

- Python 3.12 or newer
- [`uv`](https://docs.astral.sh/uv/) for the recommended workspace workflow, or `pip` for editable installs
- Docker only for opt-in repository smoke tests

## Quick start

From a local checkout:

```bash
uv sync --all-packages --extra dev
uv run python -m harborrag_app.cli.main doctor --json
uv run python scripts/run_mock_pipeline.py --json
uv run pytest
```

The doctor command should return an `ok: true` response with local engine and mock-runtime diagnostics. The pipeline loads one in-memory document, parses it with the real text parser, creates one demonstration chunk, and retrieves it without network access.

For `pip`, platform notes, and optional adapter extras, see [Installation](docs/getting-started/installation.md).

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

Connector and parser configuration examples live at [`config/connectors.example.yaml`](config/connectors.example.yaml) and [`config/parsers.example.yaml`](config/parsers.example.yaml). Copy an example before editing it; the examples themselves are safe reference files, not automatically loaded application configuration.

### Configure model clients

Install the model dependencies and provide the environment variables referenced by the selected file:

```bash
uv sync --all-packages --extra dev
OPENAI_API_KEY=placeholder \
  uv run python -m harborrag_adapters.models explain config/models.example.yaml --family chat
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
  harborrag-runtime/   configuration, composition, jobs, scheduling
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
| `config/connectors.example.yaml` | Named connector definitions and environment references |
| `config/parsers.example.yaml` | Parser profiles and PDF backend chains |
| `config/models.example.yaml` | Minimal chat, embedding, and reranking model configuration |
| `config/models.advance.example.yaml` | More advanced routing and provider examples |
| `config/advance_chat/*.example.yaml` | Direct SDK, LiteLLM Router, proxy, and distributed chat examples |
| `.env.connector.example` | Connector and connector-smoke environment template |
| `.env.parser.example` | Optional parser/OCR environment template |
| `.env.models.example` | Model and model-smoke environment template |
| `.env.database.example` | Local repository-stack template |

HarborRAG does not automatically load these environment files. Export variables in the shell or load them through your application, container runtime, or secret manager.

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
uv run make mock-pipeline
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
