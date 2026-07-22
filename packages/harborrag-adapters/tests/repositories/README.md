# Repository adapter tests

This module owns vector, graph, cache, object-store, database, workflow-state,
provider-map, lifecycle, and repository telemetry behavior.

| Type | Scope |
| --- | --- |
| `unit/` | Provider behavior with in-memory, temporary local, or fake SDK dependencies |
| `integration/` | Control-plane migrations and SQLAlchemy repositories composed over real local SQLite |
| `smoke/` | Live SQLite, PostgreSQL, Redis, Qdrant, and FalkorDB operations |

Run all repository pytest coverage with:

```bash
python -m pytest packages/harborrag-adapters/tests/repositories
```

The smoke suite can mutate external services and requires a reviewed disposable
database stack, client extras, ports, and credentials. Follow the complete
[repository smoke setup](smoke/README.md) before running it.
