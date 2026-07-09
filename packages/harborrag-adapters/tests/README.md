# Adapter Test Strategy

The adapters suite is organized by execution scope first, then by specific
behavior inside each scope. Keep root-level files limited to shared fixtures and
helpers; avoid catch-all files such as `test_performance.py`,
`test_security.py`, or `test_coverage_boost.py`.

```text
tests/
  smoke/        standalone real connector smoke scripts driven by repo-root .env
  unit/         hermetic parser, connector, base, registry, and test-double tests
  failure/      hermetic error normalization and recovery tests
  e2e/          local/fake-client public workflow tests
  integration/  cross-package composition and contract tests
  performance/ deterministic scale and resource-safety checks
  security/     hardening and hostile-input checks
```

Strategy markers:

```text
blackbox  public API behavior only
graybox   public behavior with observable internal signals, logs, routes, or fake clients
whitebox  internal architecture, private helpers, route tables, and contract internals
```

Provider tests that use fake clients belong in `unit/` with `graybox`. Smoke
scripts use `HarborConnector` against real providers. They do not use pytest.
They load repo-root `.env` values, print the discovered records and loaded
`RawDocument`, and return a non-zero exit code when a provider is not configured
or fails to load data.

Run one provider smoke test:

```bash
python packages/harborrag-adapters/tests/smoke/jira.py
```

Run every configured provider smoke test:

```bash
python packages/harborrag-adapters/tests/smoke/run_all.py
```

When a file grows to cover multiple behaviors, split it by the thing under test:
for example HTTP utility tests, attachment processing tests, PDF memoization
tests, same-origin URL tests, and secret redaction tests should each live in
their own file.
