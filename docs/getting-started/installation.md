# Installation

## Requirements

- Python 3.12 or newer
- `uv` for the recommended workflow, or a recent `pip`
- Native libraries required by any optional parser/provider you select

HarborRAG is a workspace of seven active packages under `packages/`.

## uv workspace

Install the active packages and root development tools:

```bash
uv sync --all-packages --extra dev
```

Run tools through the managed environment:

```bash
uv run python -m harborrag_app.cli.main doctor --json
uv run pytest
```

CI uses `uv sync --all-packages --all-extras`, which installs heavy and provider-specific extras. Prefer the smaller development install unless you need every PDF engine, repository SDK, and telemetry integration.

## pip editable install

The Makefile installs packages in dependency order:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
make bootstrap
```

The equivalent explicit sequence is:

```bash
python -m pip install -e packages/harborrag-core
python -m pip install -e packages/harborrag-adapters
python -m pip install -e packages/harborrag-engine
python -m pip install -e packages/harborrag-runtime
python -m pip install -e packages/harborrag-app
python -m pip install -e packages/harborrag-mcp-server
python -m pip install -e packages/harborrag
python -m pip install -e ".[dev]"
```

## Adapter extras

`harborrag-adapters` keeps heavyweight dependencies optional:

```bash
python -m pip install -e "packages/harborrag-adapters[parsers]"
python -m pip install -e "packages/harborrag-adapters[pdf]"
python -m pip install -e "packages/harborrag-adapters[pdf-docling]"
python -m pip install -e "packages/harborrag-adapters[llm]"
python -m pip install -e "packages/harborrag-adapters[redis,qdrant,falkordb,postgres,s3]"
```

Install only the families used by your application. Some PDF backends also download models or require platform-specific runtimes.
Use `pdf-docling` when the deployment only needs Docling with RapidOCR; the
aggregate `pdf` extra installs every supported PDF backend.
The `pdf` extra includes RapidOCR and the CPU `onnxruntime` package; Docling can
independently use CUDA, MPS, or XPU through an accelerator-enabled PyTorch
installation.

## Verify the checkout

```bash
uv run python -m harborrag_app.cli.main doctor --json
uv run python scripts/check_dependency_direction.py
```

These commands do not require network access or external services once dependencies are installed.

## Common install issues

- `ModuleNotFoundError: harborrag_*`: run commands through `uv run`, activate the expected virtual environment, or reinstall editable packages.
- Optional parser import error: install the matching adapter extra and any native/model prerequisites documented by that backend.
- Model configuration fails while loading: referenced environment variables are resolved eagerly and must be non-empty.
- `uv` uses an unwritable global cache in a restricted environment: set `UV_CACHE_DIR` to a writable project or temporary directory before running `uv`.

See [Troubleshooting](../users/troubleshooting/README.md) for runtime and quality-gate issues.
