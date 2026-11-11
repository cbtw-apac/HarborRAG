# Installation

Choose how to install HarborRAG's Python packages, which extras to add, and which parser
backends to install. To get started, clone the repository and follow the
[Quick Start](quick-start.md): that is the path these docs describe. This page is the
reference behind it.

## Requirements

- Python 3.12. The repository pins it in `.python-version`, and uv downloads it when it is
  missing. The PyPI packages require Python 3.12 or newer.
- [uv](https://docs.astral.sh/uv/getting-started/installation/) for the repository workflow
  (Option A), or a recent `pip` and `make` (Option B).
- [Docker Engine with Compose v2](https://docs.docker.com/get-started/get-docker/) for the
  local data and Temporal stacks.
- The native libraries an optional parser or provider needs.

## Install from a clone of the repository

```bash
git clone https://github.com/cbtw-apac/HarborRAG.git
cd HarborRAG
```

Then set up the environment one of two ways. They are alternatives, not successive steps:
running both installs the same packages twice, into different environments.

| | Option A: `uv` | Option B: `pip` and the Makefile |
| --- | --- | --- |
| Requires | [uv](https://docs.astral.sh/uv/) | `python -m venv` and `make` |
| Creates | `.venv/`, managed by uv | `.venv/`, which you activate yourself |
| Run commands with | `uv run <command>` | `<command>`, after activating |
| Use when | You follow the guides or contribute; the [Quick Start](quick-start.md), [Running HarborRAG from a checkout](../developers/checkout-quick-start.md) and CI use it | You need a plain editable install, or uv is unavailable |

Pick Option A unless something rules it out. Either option also gives you the `config/`
examples, the `scripts/deployment/dev.sh` service stack and the test suite, which the PyPI
packages do not ship.

### Option A: uv

```bash
uv sync --all-packages --extra production
```

This installs every workspace package with the clients the `harborrag` command needs to reach
the local stack: Temporal, PostgreSQL, Qdrant, FalkorDB and MinIO. Add `--extra dev` for the
lint, type-check and test tools. `--extra dev` on its own leaves out those stack clients, so
`harborrag ingestions start` cannot reach Temporal.

Run tools through the managed environment, with no activation step:

```bash
uv run harborrag --help
uv run pytest
```

CI syncs with `uv sync --all-packages --all-extras`, which pulls every heavy and
provider-specific extra. Use it only when you need every PDF engine, repository SDK and
telemetry integration.

### Option B: editable pip install

Create and activate a virtual environment, then let the Makefile install the packages in
dependency order:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
make bootstrap
```

Commands run directly once the environment is active:

```bash
harborrag --help
pytest
```

`make bootstrap` runs these commands. Note the extras, which a bare package-by-package install
would leave out:

```bash
python -m pip install -e packages/harborrag-core
python -m pip install -e "packages/harborrag-adapters[control-plane]"
python -m pip install -e packages/harborrag-engine
python -m pip install -e "packages/harborrag-runtime[production]"
python -m pip install -e "packages/harborrag-app[api]"
python -m pip install -e packages/harborrag-mcp-server
python -m pip install -e packages/harborrag
python -m pip install -e ".[dev]"
```

`harborrag-app` depends on `harborrag-memory`, so conversation memory is installed with the
CLI. `make bootstrap` installs no Temporal client, so `harborrag ingestions start` and the
other commands that reach Temporal also need the `temporal` extra of `harborrag-runtime`:

```bash
python -m pip install -e "packages/harborrag-runtime[production,temporal]"
```

See [Contributing](../../CONTRIBUTING.md) for the quality gates and the full development
setup.

## Install from PyPI

The PyPI packages are for using HarborRAG as a Python library, through the
[Python SDK](../users/python-sdk/README.md). The operator CLI, `harborrag`, runs from a
repository checkout, as in the [Quick Start](quick-start.md).

PyPI has HarborRAG 2.0.0a2, released on 2026-10-26. It predates what these docs describe, so
commands and settings on these pages may be missing from it or behave differently; see the
[changelog](../../CHANGELOG.md). Install from PyPI only when you need that published release.

For the SDK with the clients of the local stack, install the `local` extra:

```bash
pip install "harborrag[local]"
```

HarborRAG ships eight packages. `harborrag` is the facade that pulls in the rest:

```bash
pip install harborrag
```

A bare install brings `harborrag-app` (which includes `harborrag-memory`), `harborrag-core`
and `harborrag-runtime`. It does not include the MCP server, and it has none of the clients
for the local stack: add an extra for those.

### Choose the extras you need

Most extras add only the third-party clients their providers require. Four, `cli`, `server`,
`mcp` and `memory`, add first-party HarborRAG packages instead. The extra names below are the
same in this repository and in 2.0.0a2.

| Install | Adds | Use it when |
| --- | --- | --- |
| `harborrag` | the `harborrag` command, the runtime and the public facade | you supply the provider clients yourself |
| `harborrag[local]` | Qdrant, FalkorDB, S3, model client, chunking and code grammars, control plane, parsers, LiteParse PDF and tables | you use the SDK against the local stack |
| `harborrag[chat]` | model client | chat completion, embeddings and reranking |
| `harborrag[cli]` | `harborrag-app`, already part of the bare install | pinning the CLI explicitly |
| `harborrag[server]` | `harborrag-app[api]` and the reader providers | running the HTTP API |
| `harborrag[mcp]` | `harborrag-mcp-server` with its reader providers | exposing MCP tools to an IDE or agent |
| `harborrag[memory]` | `harborrag-memory`, already part of the bare install | pinning conversation memory explicitly |
| `harborrag[temporal]` | Temporal client | durable orchestration: `ingestions start`, `pause`, `resume`, `cancel` |
| `harborrag[qdrant]` | `qdrant-client` | Qdrant vector storage |
| `harborrag[falkordb]` | `falkordb` | FalkorDB graph storage |
| `harborrag[postgres]` | `asyncpg` plus the control plane | a PostgreSQL-backed control plane |
| `harborrag[s3]` | `aiobotocore` | S3 artifact storage |
| `harborrag[redis]` | `redis` | Redis-backed features |
| `harborrag[all]` | every extra above | you want the full surface |

Extras combine, so install exactly the set you need:

```bash
pip install "harborrag[qdrant,falkordb,chat]"
```

`harborrag[all]` is a superset of `harborrag[local]`.

### Chat and embeddings need a model client

Chat, embedding and reranking all go through the model client in the `chat` extra. Without
`harborrag[chat]`, or an extra that includes it such as `local`, `server` or `all`, those
calls fail on a missing import even though the rest of HarborRAG works.

### Install individual packages

The facade is a convenience. Any package can be installed on its own for a narrower
dependency tree:

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
pip install "harborrag-mcp-server[reader]"
```

Each package ships its own README with usage details, published in the package reference
section of the documentation.

## Run the installed commands

| Command | Provided by | Available with |
| --- | --- | --- |
| `harborrag` | `harborrag-app` | `harborrag`, with or without extras |
| `harborrag-mcp` | `harborrag-mcp-server` | `harborrag[mcp]` or `harborrag[all]` |
| `harborrag-mcp-ui` | `harborrag-mcp-server` | `harborrag-mcp-server[ui]` |

A PyPI install puts these on your `PATH`. `harborrag --help` works anywhere, but the other
`harborrag` commands run only inside a repository checkout; elsewhere they stop with
`harborrag: no project found. Run it inside a HarborRAG repository checkout, ...`.

In a repository checkout, `uv sync --all-packages` installs the console scripts into the
workspace environment, so prefix them with `uv run`:

```bash
uv run harborrag --help
uv run harborrag-mcp --help
```

If you synced without `--all-packages`, a script can be missing from the environment: add the
selector back (`uv run --package harborrag-app harborrag --help`) or re-sync with
`--all-packages`.

## Add parser and PDF extras

`harborrag-adapters` keeps document parsing optional. Install only the families your content
needs:

```bash
pip install "harborrag-adapters[parsers]"        # text, Office and image formats, PyMuPDF, LiteParse
pip install "harborrag-adapters[pdf-docling]"    # Docling with RapidOCR
pip install "harborrag-adapters[pdf]"            # every supported PDF backend
pip install "harborrag-adapters[parsers-all]"    # parsers plus every PDF backend
```

Narrower extras exist for single backends: `pdf-pymupdf`, `pdf-liteparse`, `pdf-mineru`,
`pdf-ocr`, `document`, `spreadsheet`, `presentation`, `markup`, `image`, `image-tesseract`
and `image-rapidocr`.

In a checkout with Option B, use the editable form:

```bash
python -m pip install -e "packages/harborrag-adapters[parsers]"
```

Some PDF backends download models or need platform-specific runtimes. The `pdf` extra
includes RapidOCR and the CPU `onnxruntime` package; Docling can independently use CUDA, MPS
or XPU through an accelerator-enabled PyTorch installation.

## Verify the install

In a checkout:

```bash
uv run harborrag doctor
uv run python scripts/check_dependency_direction.py
```

`harborrag doctor` checks the checkout's catalogs and the local services, Temporal included.
Its service checks fail until the stack is running, so run it after
[starting the local stack](quick-start.md#3-start-the-local-stack). In a checkout, the
`source path` and `connector credentials` checks also fail until `env/.env.connector` sets
`LOCAL_SOURCE_PATH` and the remote connections' credentials.

After a PyPI install, check that the SDK imports:

```bash
python -c "import harborrag; print(harborrag.__all__)"
```

It prints the names the public facade exports, such as `HarborRAG` and `RetrievalRequest`.

## Fix common install issues

- `ModuleNotFoundError` for a provider client: install the extra that supplies it, for example
  `harborrag[qdrant]` or `harborrag[chat]`.
- `ModuleNotFoundError: harborrag_*` in a checkout: run commands through `uv run`, activate
  the expected virtual environment, or reinstall the editable packages.
- `harborrag ingestions start` or another stack command fails on a missing module such as
  `temporalio` in a checkout: sync with `--extra production` (Option A), or install the
  `temporal` extra of `harborrag-runtime` (Option B).
- Optional parser import error: install the matching adapter extra, plus any native or model
  prerequisites that backend documents.
- Model configuration fails with `environment variable <NAME> is not set`: every `${NAME}` in
  `config/models/models.yaml` is expanded when the file loads, so each one must be set. A set
  but blank value loads and fails later at the provider; `harborrag doctor` reports it as blank.
- uv uses an unwritable global cache in a restricted environment: set `UV_CACHE_DIR` to a
  writable project or temporary directory.

See [Troubleshooting](../users/troubleshooting/README.md) for runtime and quality-gate issues.
