# Repository adapter tests

These tests cover the vector, graph, cache, object-store, database and workflow-state
repositories, the provider map, repository lifecycle and repository telemetry.

| Folder | What it covers |
| --- | --- |
| `unit/` | Provider behaviour with in-memory, temporary local or fake SDK dependencies |
| `integration/` | Control-plane migrations and SQLAlchemy repositories over a temporary local SQLite database |
| `smoke/` | Standalone scripts that run real SQLite, PostgreSQL, Redis, Qdrant and FalkorDB operations |

Run the default selection from the repository root:

```bash
uv run pytest packages/harborrag-adapters/tests/repositories
```

The default run leaves out tests marked `integration`, which is most of `integration/`. They need
no external service, so run them explicitly:

```bash
uv run pytest packages/harborrag-adapters/tests/repositories/integration -m integration
```

## Check the repositories coverage gate

`harborrag_adapters.repositories` must stay at 90% line and branch coverage. Measure it with the
unit and integration tests together, because the SQL repositories are covered only by the
integration tests; the unit tests alone reach about 79%:

```bash
uv run pytest -n 0 packages/harborrag-adapters/tests/repositories -m "not e2e and not smoke" \
  --cov=harborrag_adapters.repositories --cov-fail-under=90
```

The run takes about five minutes and exits non-zero below 90%.

The smoke scripts write to real services, so they need a disposable database stack, the client
libraries, open ports and credentials. Set them up with
[Repository smoke checks](smoke/README.md) before you run them.
