# What is HarborRAG?

HarborRAG is a provider-agnostic Retrieval-Augmented Generation framework for engineering knowledge. It uses ports and adapters so source systems, document formats, model providers, persistence backends, and operator surfaces can change independently.

## Capability status

HarborRAG is under active alpha development. The distinction between implemented adapters and the default application path matters:

| Layer | Status |
| --- | --- |
| Core domain and schemas | Implemented provider-neutral document, retrieval, model, storage, tenancy, security, and telemetry contracts |
| Source connectors | Implemented for local files, GitHub, Confluence, Jira, and SharePoint |
| Document parsers | Implemented for common text, structured, Office, image, ebook, and PDF formats |
| Model adapters | Implemented chat, embedding, and reranking clients with validation, routing, retries, caching, budgets, and telemetry boundaries |
| Repository adapters | Implemented backends for Qdrant, FalkorDB, Redis, PostgreSQL, SQLite, S3, filesystem, and memory |
| Engine and runtime | Contracts and focused utilities exist; the default composition is a local mock/smoke path rather than a production ingestion service |
| CLI | `doctor` and `sample-ingest` work; full ingest, retrieve, and status commands are stubs |
| HTTP API | Controller contracts and route placeholders exist; there is no FastAPI application factory yet |
| MCP | In-process mock tools work; there is no stdio or HTTP transport yet |
| Temporal and memory package | Reserved scaffolding, not operational subsystems |

## Why the package boundaries exist

Engineering RAG combines several failure domains: source authentication and pagination, hostile or malformed documents, model routing, tenant-aware persistence, retries, orchestration, and public interfaces. Keeping them separate provides three practical benefits:

- provider SDKs do not leak into domain contracts or orchestration;
- most behavior can be tested without live credentials or services;
- applications can choose providers independently for each capability family.

The package dependency direction is checked in CI. See [Architecture](../developers/architecture/README.md) for the exact graph.

## Intended use today

Use the repository to develop and test adapters, schemas, and orchestration boundaries; evaluate local behavior through the mock pipeline; and run opt-in real-system smoke checks in a controlled environment. A production application still needs composition, service endpoints, authentication/authorization, and operational hardening around these packages.

## Next steps

- [Installation](installation.md)
- [Quick Start](quick-start.md)
- [Configuration](../users/configuration/README.md)
- [Extending HarborRAG](../developers/extending/README.md)
