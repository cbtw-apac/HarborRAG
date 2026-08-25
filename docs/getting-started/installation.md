# Installation

HarborRAG supports two installation methods:

1. **[From the repository](#method-1-install-from-the-repository)** — clone and
   install. Available now, and the method to use for contributing.
2. **[From PyPI](#method-2-install-from-pypi)** — `pip install harborrag` with
   the extras you need. Published with the `2.0.0a1` release.

Both give the same packages. Pick the repository method if you want the example
configuration, deployment scripts, and test suite; pick PyPI if you are adding
HarborRAG to an existing project.

## Requirements

- Python 3.12 or newer
- [`uv`](https://docs.astral.sh/uv/) for the repository workflow, or a recent `pip`
- Docker Engine with Compose v2 for the local data and Temporal stacks
- Native libraries required by any optional parser/provider you select

## Method 1: install from the repository

```bash
git clone https://github.com/cbtw-apac/HarborRAG.git
cd HarborRAG
uv sync --all-packages --extra dev
```

Run tools through the managed environment:

```bash
uv run --package harborrag-app harborrag --help
uv run pytest
```

This checkout also gives you `config/*.yaml` examples, the
`scripts/deployment/dev.sh` service stack, and the test suite, which the PyPI
packages do not ship. [Quick Start](quick-start.md) follows this path.

CI syncs with `uv sync --all-packages --all-extras`, pulling every heavy and
provider-specific extra. Prefer `--extra dev` unless you need every PDF engine,
repository SDK, and telemetry integration.

### Editable pip install

The Makefile installs the packages in dependency order:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
make bootstrap
```

The equivalent explicit sequence:

```bash
python -m pip install -e packages/harborrag-core
python -m pip install -e packages/harborrag-adapters
python -m pip install -e packages/harborrag-engine
python -m pip install -e packages/harborrag-memory
python -m pip install -e packages/harborrag-runtime
python -m pip install -e packages/harborrag-app
python -m pip install -e packages/harborrag-mcp-server
python -m pip install -e packages/harborrag
python -m pip install -e ".[dev]"
```

See [Contributing](../../CONTRIBUTING.md) for quality gates and the full
development setup.

## Method 2: install from PyPI

> The packages below are published as part of the `2.0.0a1` alpha release and
> are not yet on PyPI. Until then, use
> [Method 1](#method-1-install-from-the-repository).

HarborRAG ships eight packages. `harborrag` is the facade that pulls in the
rest:

```bash
pip install harborrag
```

A bare install gives you the facade, the provider-neutral contracts, and the
adapter code. It deliberately installs **no provider clients**, so there is no
vector store, no graph store, and no model client until you add an extra.

Everything at once:

```bash
pip install "harborrag[all]"
```

### Which extra do I need?

Each extra adds only the third-party clients its providers require.

| Install | Adds | Use it when |
| --- | --- | --- |
| `harborrag` | contracts only, plus SQLAlchemy/SQLite | you supply your own adapters |
| `harborrag[local]` | Qdrant, FalkorDB, S3, model client, chunking, control plane, parsers, Docling PDF, tables | local end-to-end ingestion and retrieval |
| `harborrag[chat]` | model client | chat completion, embeddings, reranking |
| `harborrag[cli]` | `harborrag-app` | the `harborrag` command |
| `harborrag[server]` | `harborrag-app[api]`, production and Temporal runtime | running the HTTP API |
| `harborrag[mcp]` | `harborrag-mcp-server[mcp]` | exposing MCP tools to an IDE or agent |
| `harborrag[memory]` | `harborrag-memory` | conversation memory |
| `harborrag[temporal]` | Temporal client | durable orchestration, `submit`/`pause`/`resume`/`cancel` |
| `harborrag[qdrant]` | `qdrant-client` | Qdrant vector storage |
| `harborrag[falkordb]` | `falkordb` | FalkorDB graph storage |
| `harborrag[postgres]` | `asyncpg` plus the control plane | PostgreSQL-backed control plane |
| `harborrag[s3]` | `aioboto3` | S3 artifact storage |
| `harborrag[redis]` | `redis` | Redis-backed features |
| `harborrag[all]` | every extra above | you want the full surface |

Extras combine, so install exactly the set you need:

```bash
pip install "harborrag[cli,qdrant,falkordb,chat]"
```

`harborrag[all]` is a superset of `harborrag[local]`.

### Chat and embeddings need a model client

Chat, embedding, and reranking all route through the model client in the `chat`
extra. Without `harborrag[chat]` — or an extra that includes it, such as
`local`, `server`, or `all` — those calls fail on a missing import even though
the rest of HarborRAG works.

### Installing individual packages

The facade is a convenience. Any package can be installed on its own for a
narrower dependency tree:

| Package | Contains |
| --- | --- |
| `harborrag` | public facade and install bundle |
| `harborrag-core` | provider-neutral contracts and domain |
| `harborrag-adapters` | connectors, parsers, stores, model clients |
| `harborrag-engine` | ingestion and retrieval engine |
| `harborrag-memory` | conversation memory |
| `harborrag-runtime` | runtime and Temporal orchestration |
| `harborrag-app` | CLI and HTTP API |
| `harborrag-mcp-server` | MCP transport |

For example, an MCP-only deployment:

```bash
pip install "harborrag-mcp-server[mcp]"
```

Each package ships its own README with usage details, published under the
package reference section of the documentation.

## Commands

| Command | Provided by | Available with |
| --- | --- | --- |
| `harborrag` | `harborrag-app` | `harborrag[cli]`, `harborrag[server]`, `harborrag[all]` |
| `harborrag-mcp` | `harborrag-mcp-server` | `harborrag[mcp]`, `harborrag[all]` |

A PyPI install puts these on your `PATH`, so `harborrag --help` works directly.
In a repository checkout, prefix them with
`uv run --package harborrag-app` (or `--package harborrag-mcp-server`) so they
resolve inside the workspace environment.

## Parser and PDF extras

`harborrag-adapters` keeps document parsing optional. Install only the families
your content needs:

```bash
pip install "harborrag-adapters[parsers]"        # common text, Office, image formats
pip install "harborrag-adapters[pdf-docling]"    # Docling with RapidOCR
pip install "harborrag-adapters[pdf]"            # every supported PDF backend
pip install "harborrag-adapters[parsers-all]"    # parsers plus every PDF backend
```

Narrower extras exist for single backends: `pdf-pymupdf`, `pdf-liteparse`,
`pdf-mineru`, `pdf-ocr`, `document`, `spreadsheet`, `presentation`, `markup`,
`image`, `image-tesseract`, and `image-rapidocr`.

In a checkout, use the editable form:

```bash
python -m pip install -e "packages/harborrag-adapters[parsers]"
```

Some PDF backends download models or need platform-specific runtimes. The `pdf`
extra includes RapidOCR and the CPU `onnxruntime` package; Docling can
independently use CUDA, MPS, or XPU through an accelerator-enabled PyTorch
installation.

## Verify the install

```bash
python -c "import harborrag; print(harborrag.__all__)"
```

In a checkout:

```bash
uv run python -m harborrag_app.cli.main doctor --json
uv run python scripts/check_dependency_direction.py
```

`harborrag doctor` is a live Temporal health check, so run it after starting the
services in [Quick Start](quick-start.md).

## Common install issues

- `ModuleNotFoundError` for a provider client: install the extra that supplies
  it, for example `harborrag[qdrant]` or `harborrag[chat]`.
- `ModuleNotFoundError: harborrag_*` in a checkout: run commands through
  `uv run`, activate the expected virtual environment, or reinstall the editable
  packages.
- Optional parser import error: install the matching adapter extra plus any
  native or model prerequisites that backend documents.
- Model configuration fails while loading: referenced environment variables are
  resolved eagerly and must be non-empty.
- `uv` uses an unwritable global cache in a restricted environment: set
  `UV_CACHE_DIR` to a writable project or temporary directory.

See [Troubleshooting](../users/troubleshooting/README.md) for runtime and
quality-gate issues.
