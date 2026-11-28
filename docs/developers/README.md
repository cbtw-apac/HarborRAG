# Developer Documentation

This section is for contributors who change HarborRAG's code and for operators who deploy it.
Set up a checkout with the [Quick Start](../getting-started/quick-start.md) first.

## Pages in this section

- [Running HarborRAG from a checkout](checkout-quick-start.md) - explore the code without
  services, start and rebuild one service at a time, drive ingestion runs and run the tests.
- [Architecture](architecture/README.md) - package ownership, dependency direction and the
  rules that hold across packages.
  - [Data lifecycle](architecture/data-lifecycle.md) - authority, artifacts, projections,
    publication and retrieval.
  - [Runtime reliability](architecture/runtime-reliability.md) - Temporal boundaries, replay,
    retries and safe failure behaviour.
  - [Projection rebuild](architecture/projection-rebuild.md) - what each store holds and how
    the search projections are rebuilt.
- [Extending HarborRAG](extending/README.md) - connectors, parsers, models, repositories,
  engine stages and public surfaces.
- [Testing](testing/README.md) - test layout, markers, coverage and real-system smoke checks.
- [Deployment](deployment/README.md) - the local service stacks, environment files,
  monitoring, the CLI and MCP images and the production boundary.
- [Release process](release-process.md) - the maintainer-only publication sequence.
- [Open-source publication guidelines](publication-guidelines.md) - what belongs in public
  docs and what must stay private.
- [Contributing](../../CONTRIBUTING.md) - setup, quality gates, commits and pull requests.

## Development principles

- Keep provider-neutral data and errors in `harborrag-core`; keep provider SDKs in
  `harborrag-adapters`.
- Prefer narrow dependency injection over a global settings object.
- Preserve source identity, permissions, content type, timestamps and parser warnings across
  ingestion stages.
- Treat the embedding model and its dimension as part of a vector space's identity.
- Require explicit tenant context on repository operations.
- Keep default tests hermetic; put live checks behind opt-in smoke scripts.
- Redact secrets and bound logged content at every provider and observability boundary.
- Document implemented behaviour separately from scaffolded intent.

Before opening a pull request, run the
[quality gates](../../CONTRIBUTING.md#quality-gates).
