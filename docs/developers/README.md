# Developer Documentation

- [Architecture](architecture/README.md) — package ownership, dependency direction, and implemented boundaries.
  - [Data lifecycle](architecture/data-lifecycle.md) — authority, artifacts, projections, publication, and retrieval.
  - [Runtime reliability](architecture/runtime-reliability.md) — Temporal boundaries, replay, retries, and safe failure behavior.
- [Extending HarborRAG](extending/README.md) — connectors, parsers, models, repositories, engine stages, and public surfaces.
- [Testing](testing/README.md) — test layout, markers, quality gates, and real-system smoke checks.
- [Deployment](deployment/README.md) — local service stacks plus API, CLI, worker, and MCP images.
- [Open-source publication guidelines](publication-guidelines.md) — what belongs in public docs and what must stay private.
- [Contributing](../../CONTRIBUTING.md) — setup, review expectations, commits, and pull requests.

## Development principles

- Keep provider-neutral data and errors in `harborrag-core`; keep provider SDKs in `harborrag-adapters`.
- Prefer narrow dependency injection over a global settings object.
- Preserve source identity, permissions, content type, timestamps, and parser warnings across ingestion stages.
- Treat embedding model and dimension as part of a vector space's identity.
- Require explicit tenant context on repository operations.
- Make default tests hermetic; put live checks behind opt-in smoke scripts.
- Redact secrets and bound logged content at every provider/observability boundary.
- Document implemented behavior separately from scaffolded intent.

Before opening a pull request, run the quality commands in [CONTRIBUTING.md](../../CONTRIBUTING.md#quality-gates).
