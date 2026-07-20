# Testing

Every package owns its own test folder. There is no shared/global test suite
that mixes packages:

```text
tests/                              cross-package + website build tests
packages/harborrag-core/tests/
packages/harborrag-adapters/tests/
packages/harborrag-engine/tests/
packages/harborrag-runtime/tests/
packages/harborrag-app/tests/
packages/harborrag-mcp/tests/
packages/harborrag/tests/
```

Add tests next to the package you modify. The root `tests/` folder is reserved
for checks that span packages or repository-wide architecture rules.

## Running tests

```bash
pytest                                        # everything
pytest --cov --cov-report=term-missing        # with coverage
make test-package PACKAGE=harborrag-core      # a single package
```

`make test` runs the same as plain `pytest`; both are driven by
`[tool.pytest.ini_options]` in the workspace `pyproject.toml`, which lists every
package's `tests/` directory under `testpaths`.

## Coverage gate

```bash
make coverage
```

`[tool.coverage.report].fail_under = 90` in `pyproject.toml`; coverage below
90% fails the command and CI's `quality-gates.yml`. `[tool.coverage.run].source`
lists every package's `src/` directory, so coverage is measured on
implementation code, not test code.

## Markers

Defined in `[tool.pytest.ini_options].markers`:

```text
slow          marks tests as slow (deselect with '-m not slow')
integration   marks tests that compose packages or require environment-dependent boundaries
unit          marks unit tests
smoke         smoke checks; adapter provider smokes are standalone scripts
blackbox      public API behavior tests
graybox       public behavior tests that assert observable internal signals
whitebox      internal architecture and contract tests
requires_deps marks tests requiring optional dependencies
workflow      marks tests that validate GitHub Actions workflow files
contract      reusable behavioral contracts shared by implementations
chaos         deterministic fault-injection and recovery tests
performance   bounded local performance and concurrency tests
load          correctness-oriented local micro-load tests
```

Tests requiring Docker, cloud credentials, provider accounts, or live model APIs
must be marked `@pytest.mark.integration`.

`packages/harborrag-adapters/tests/smoke/` contains standalone real-system
checks for connectors, model providers, and real parser inputs. They do not use
pytest or mocks and are not part of normal test discovery. Copy the relevant
groups from `.env.connector.example` and `.env.models.example` into repo-root
`.env`, then run a configured group:

```bash
python packages/harborrag-adapters/tests/smoke/connectors/run_all.py
python packages/harborrag-adapters/tests/smoke/models/run_all.py
HARBOR_SMOKE_ENV_FILE=env/.env.database \
  python packages/harborrag-adapters/tests/smoke/repositories/run_all.py
```

Run a single provider script when only one connector is configured:

```bash
python packages/harborrag-adapters/tests/smoke/connectors/jira.py
```

Real parser inputs are checked individually:

```bash
python packages/harborrag-adapters/tests/smoke/parsers/parse_file.py samples/report.pdf --pdf-profile fast
```

See the adapter [real smoke-test runbook](../../../packages/harborrag-adapters/tests/smoke/README.md)
for prerequisites, safe output rules, exit codes, and the list of checks that
may remain unavailable outside the main environment.

## Testing contracts and providers

Each provider family exposes a stable `base.py` contract. Package-local tests
exercise production implementations with deterministic in-memory or fake SDK
clients (see [Architecture Overview](../architecture/README.md#provider-contracts-and-test-doubles)):

1. construct the real adapter with validated configuration and a fake provider client;
2. exercise its public contract, including tenant isolation and lifecycle behavior;
3. assert both the normalized core schema and the request sent to the provider;
4. cover conflict, timeout, expiration, and partial-failure paths without live credentials.

`harborrag-core`'s `testing/fakes.py` (`FakeConnector`, `FakeParser`) exists for
tests in other packages that need connector/parser behavior without importing
`harborrag-adapters`.

Standalone smoke scripts complement the hermetic suite by checking deployed
services through the same public APIs. They never run during normal pytest
discovery.

## Quality gates as a whole

```bash
make lint          # ruff check .
make typecheck      # mypy packages
make compile        # python -m compileall packages scripts
make deps-check      # scripts/check_dependency_direction.py
make coverage
```

All five run in `.github/workflows/quality-gates.yml` on every push/PR to
`main`. `.github/workflows/test.yml` additionally runs each package's test suite
in its own matrix job, plus the website build tests.

## Related

- [Architecture Overview](../architecture/README.md) - the package boundaries these tests protect.
- [Extending HarborRAG](../extending/README.md) - how to test a real provider you're adding.
