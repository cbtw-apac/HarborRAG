# Testing

This page shows where HarborRAG's tests live, how to run them, which markers exist, and how to
run the opt-in checks against real systems. The suite runs on
[pytest](https://docs.pytest.org/en/stable/); the default run needs no network, Docker, cloud
account, paid API, ambient credentials or model download.

## Test ownership

Each package tests its own behaviour:

```text
tests/                                repository-wide and website tests
packages/harborrag-core/tests/        core contracts and schemas
packages/harborrag-adapters/tests/    connectors, parsers, models, repositories
packages/harborrag-engine/tests/      engine policy, tools and utilities
packages/harborrag-memory/tests/      conversation memory
packages/harborrag-runtime/tests/     config loaders and runtime boundaries
packages/harborrag-app/tests/         CLI, HTTP and app boundaries
packages/harborrag-mcp-server/tests/  MCP boundaries
packages/harborrag/tests/             meta-package exports
```

## Run the tests

```bash
uv run pytest
uv run make test
uv run make test-package PACKAGE=harborrag-adapters
uv run make coverage
uv run make coverage-html
```

By default pytest deselects tests marked `integration`, `e2e` or `smoke`, and runs on eight
[pytest-xdist](https://pytest-xdist.readthedocs.io/en/stable/) workers. Pass `-n 0` to run
serially. Pytest settings, test paths, markers, warning filters and the coverage source list
are in the root `pyproject.toml`.

While you develop, run the narrowest path that covers your change:

```bash
uv run pytest packages/harborrag-adapters/tests/connectors/unit/
uv run pytest packages/harborrag-runtime/tests/test_connector_config/
```

A `-m` expression on the command line replaces the default selection rather than adding to
it, so repeat the default exclusions when you add your own:

```bash
uv run pytest -m "not integration and not e2e and not smoke and not slow"
```

## Coverage

`make coverage` measures branch coverage over every package's `src/` folder and fails below
90%. Tests, virtual environments, generated caches and reports are omitted.

Do not add broad `pragma: no cover` exclusions for testable logic. Abstract methods,
protocols, `TYPE_CHECKING` branches and `raise NotImplementedError` lines are already
excluded.

## Markers

The markers registered in `pyproject.toml` are:

| Marker | Use |
| --- | --- |
| `unit` | Fast, isolated, no I/O |
| `contract` | Port or protocol conformance suite |
| `integration` | Real adapter against a real backend |
| `smoke` | Minimal post-deploy liveness check |
| `e2e` | Full-stack scenario |
| `slow` | Slow test; deselect it with the full expression shown in [Run the tests](#run-the-tests) |
| `blackbox` | Public API behaviour only |
| `graybox` | Public behaviour plus observable internal signals |
| `whitebox` | Internal architecture and contract behaviour |
| `requires_deps` | Needs an optional dependency |
| `workflow` | Validates a GitHub Actions workflow |
| `chaos` | Deterministic fault injection and recovery |
| `performance` | Bounded local performance and concurrency |
| `load` | Correctness-oriented local micro-load |

## Adapter test strategy

The adapter suite is organized by production module first, then by test type: unit,
integration, contract, failure, security, chaos, performance or smoke. Test a provider
implementation against a deterministic fake SDK, HTTP or database dependency:

1. Build the real adapter with validated configuration.
2. Exercise its public contract and lifecycle.
3. Assert the normalized Harbor outputs.
4. Assert the outbound provider request or query.
5. Cover tenant separation, redaction, retryability, limits, conflicts and partial failures.

Avoid asserting private implementation details when a public request, result or telemetry
event captures the same invariant.

## Real-system smoke checks

Standalone scripts in each `packages/<package>/tests/<module>/smoke/` folder check a real
system: a source, a paid model provider, a local service or a heavyweight model. They are
manual and opt-in, and may read private content. Pytest never collects them, because their
names do not match `test_*.py`; the `test_*.py` files beside some of them are ordinary tests
of the smoke helpers. Run the scripts with the workspace's Python environment.

The scripts read credentials from the git-ignored `env/` folder, which
`scripts/deployment/dev.sh bootstrap` creates from the templates in `env-example/`; first-time
setup is in the [Quick Start](../../getting-started/quick-start.md). Connector checks read
`env/.env.connector` and `env/.env.parser`. Model and repository checks read the file that
`HARBOR_SMOKE_ENV_FILE` names, or a repository-root `.env` when it is unset, and never replace
a variable you already exported. Never commit populated files.

Connector checks:

```bash
python packages/harborrag-adapters/tests/connectors/smoke/local.py
python packages/harborrag-adapters/tests/connectors/smoke/jira.py
python packages/harborrag-adapters/tests/connectors/smoke/run_all.py
```

Model checks:

```bash
export HARBOR_SMOKE_ENV_FILE=env/.env.models
python packages/harborrag-adapters/tests/models/smoke/chat.py
python packages/harborrag-adapters/tests/models/smoke/embed.py
python packages/harborrag-adapters/tests/models/smoke/rerank.py
```

Parser check, on a PDF of your own:

```bash
python packages/harborrag-adapters/tests/parsers/smoke/parse_file.py path/to/report.pdf --pdf-profile fast
```

Repository checks, against the local data services:

```bash
scripts/deployment/dev.sh data
HARBOR_SMOKE_ENV_FILE=env/.env.database \
  python packages/harborrag-adapters/tests/repositories/smoke/run_all.py
```

The deployed ingestion smoke covers chunk content, PostgreSQL publication, Qdrant retrieval,
FalkorDB traversal and Temporal runtime state; its
[README](../../../packages/harborrag-runtime/tests/runtime_ingestion/smoke/README.md) lists
the prerequisites:

```bash
python packages/harborrag-runtime/tests/runtime_ingestion/smoke/ingestion_flow.py
```

A smoke script that exits with code 2 found its prerequisites missing or unconfigured; that is
not a successful provider check. Start with the
[`harborrag-adapters` test index](../../../packages/harborrag-adapters/tests/README.md), then
follow the owning module's `smoke/README.md` for advanced setup, safety rules and
target-specific variables.

Graph evaluation gates the FalkorDB knowledge graph with a conformance census, structural
health and build-to-build regression; see its
[README](../../../packages/harborrag-runtime/tests/graph_eval/README.md). Its pure modules
are covered by ordinary collected tests in `graph_eval/unit/`; only the live scripts sit
outside collection:

```bash
python packages/harborrag-runtime/tests/graph_eval/smoke/graph_health.py
```

## Quality and documentation checks

The full local quality set is listed in
[Quality gates](../../../CONTRIBUTING.md#quality-gates), and the docs-site build and checks in
[Documentation changes](../../../CONTRIBUTING.md#documentation-changes). CI runs the quality
set, one test job per package, and a combined 90% coverage gate. Website tests are part of
the test workflow, so a documentation-only change can fail on broken page discovery or
rendering.
