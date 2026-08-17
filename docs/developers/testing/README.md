# Testing

## Test ownership

```text
tests/                              website and repository-wide tests
packages/harborrag-core/tests/      core contracts and schemas
packages/harborrag-adapters/tests/  connectors, parsers, models, repositories
packages/harborrag-engine/tests/    engine contracts and utilities
packages/harborrag-runtime/tests/   config loaders and runtime boundaries
packages/harborrag-app/tests/       CLI/app boundaries
packages/harborrag-mcp-server/tests/       MCP boundaries
packages/harborrag/tests/           meta-package exports
```

## Common commands

```bash
uv run pytest
uv run make test
uv run make test-package PACKAGE=harborrag-adapters
uv run make coverage
uv run make coverage-html
```

Run the narrowest relevant path while developing:

```bash
uv run pytest packages/harborrag-adapters/tests/connectors/unit/
uv run pytest packages/harborrag-runtime/tests/test_connector_config.py
uv run pytest -m "not slow and not integration"
```

Pytest settings, discovery paths, markers, warning filters, and the coverage source list are defined in the root `pyproject.toml`.

## Coverage

`make coverage` runs branch coverage over every active package `src/` directory and fails below 90%. Tests, virtual environments, generated caches, and reports are omitted.

Do not add broad `pragma: no cover` exclusions for testable logic. Abstract methods, protocols, type-checking branches, and defensive impossible paths already have targeted policy exclusions.

## Markers

The registered markers are:

| Marker | Use |
| --- | --- |
| `unit` | Focused unit behavior |
| `integration` | Environment-dependent or composed external boundary |
| `slow` | Locally slow test |
| `smoke` | Fast wiring/import check |
| `blackbox` | Public behavior only |
| `graybox` | Public behavior plus observable internal signals |
| `whitebox` | Internal architecture/contract behavior |
| `requires_deps` | Requires an optional dependency |
| `workflow` | GitHub Actions workflow validation |
| `contract` | Reusable implementation contract |
| `chaos` | Deterministic fault injection and recovery |
| `performance` | Bounded local performance/concurrency |
| `load` | Correctness-oriented micro-load |

Default tests must not require network access, paid APIs, Docker, cloud accounts, ambient credentials, or model downloads.

## Adapter test strategy

The adapter suite is organized by production module first, then by unit,
integration, contract, failure, security, chaos, performance, or smoke type.
Test a provider implementation with a deterministic fake SDK/HTTP/database
dependency:

1. construct the real adapter with validated configuration;
2. exercise its public contract and lifecycle;
3. assert normalized Harbor outputs;
4. assert the outbound provider request/query;
5. cover tenant separation, redaction, retryability, limits, conflicts, and partial failures.

Avoid asserting private implementation details when a public request/result or telemetry event captures the same invariant.

## Real-system smoke checks

Standalone scripts under each `packages/<package>/tests/<module>/smoke/`
directory are manual and opt-in. They may access private content, paid providers, local services, or
heavyweight models. They are intentionally outside normal pytest discovery.

Connector examples:

```bash
python packages/harborrag-adapters/tests/connectors/smoke/local.py
python packages/harborrag-adapters/tests/connectors/smoke/jira.py
python packages/harborrag-adapters/tests/connectors/smoke/run_all.py
```

Model examples:

```bash
python packages/harborrag-adapters/tests/models/smoke/chat.py
python packages/harborrag-adapters/tests/models/smoke/embed.py
python packages/harborrag-adapters/tests/models/smoke/rerank.py
```

Parser example:

```bash
python packages/harborrag-adapters/tests/parsers/smoke/parse_file.py samples/report.pdf --pdf-profile fast
```

The deployed ingestion smoke covers chunk content, Postgres publication,
Qdrant retrieval, FalkorDB traversal, and Temporal runtime state:

```bash
python packages/harborrag-runtime/tests/runtime_ingestion/smoke/ingestion_flow.py
```

Repository stack and runner:

```bash
scripts/deployment/dev.sh data

HARBOR_SMOKE_ENV_FILE=env/.env.database \
  python packages/harborrag-adapters/tests/repositories/smoke/run_all.py
```

Copy `env-example/.env.database.example` to the protected path first and adjust
ports/credentials. Connector/model templates are
`env-example/.env.connector.example` and `env-example/.env.models.example`. Do
not commit populated files.

Smoke exit code 2 means prerequisites are unavailable or not configured; it is
not a successful provider check. Start with the
[`harborrag-adapters` test index](../../../packages/harborrag-adapters/tests/README.md),
then follow the owning module's `smoke/README.md` for advanced setup, safety
rules, and target-specific variables.

Graph evaluation gates the FalkorDB knowledge graph (conformance census,
structural health, build-to-build regression); see its
[`README.md`](../../../packages/harborrag-runtime/tests/graph_eval/README.md) and
`GRAPH_EVAL.md` at the repo root for the research basis. Its pure modules are
covered by ordinary collected tests in `graph_eval/unit/`; only the live scripts
below sit outside discovery:

```bash
python packages/harborrag-runtime/tests/graph_eval/smoke/graph_health.py
```

## Quality and documentation tests

```bash
uv run make lint
uv run make typecheck
uv run make deps-check
uv run make compile
uv run python website/build.py --output site --templates website/templates
uv run pytest tests/
```

CI runs the full quality set and a package matrix. Website tests are part of the test workflow, so broken document discovery or rendering can fail a documentation-only change.
