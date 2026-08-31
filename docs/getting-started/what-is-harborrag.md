# What is HarborRAG?

HarborRAG is a provider-agnostic Retrieval-Augmented Generation framework for engineering knowledge. It uses ports and adapters so source systems, document formats, model providers, persistence backends, and operator surfaces can change independently.

The first public alpha release is **HarborRAG 2.0.0 Alpha 1** (Python package
version `2.0.0a1`), dated August 27, 2026. It continues the project
lineage of Qdrant Loader as a breaking rename and a provider-neutral
architectural expansion; legacy import paths and package names do not carry
over.

## Capability status

HarborRAG is under active alpha development. The distinction between implemented adapters and the default application path matters:

| Layer | Status |
| --- | --- |
| Core domain and schemas | Implemented provider-neutral document, retrieval, model, storage, tenancy, security, and telemetry contracts |
| Source connectors | Implemented for local files, GitHub, Confluence, Jira, and SharePoint |
| Document parsers | Implemented for common text, structured, Office, image, ebook, and PDF formats |
| Model adapters | Implemented chat, embedding, and reranking clients with validation, routing, retries, caching, budgets, and telemetry boundaries |
| Repository adapters | Implemented backends for Qdrant, FalkorDB, Redis, PostgreSQL, SQLite, S3, filesystem, and memory |
| Engine and runtime | Postgres-authoritative ingestion, durable Temporal workflows, immutable artifacts, hybrid vector projection, graph projection, retrieval, chat orchestration, stored prompts, and production dependency composition are implemented |
| CLI | `doctor`, one-shot chat, Temporal-backed ingestion control, and authoritative retrieval commands |
| HTTP API | FastAPI factory serving a public `/v1` contract (ingestion, retrieval, chat, agent, admin) plus an operational `/api/v1` surface (liveness, readiness, Prometheus metrics, console routes) |
| MCP | Audited, policy-bounded FastMCP transport exposing four tenant-scoped retrieval tools (`vector_search`, `graph_triplet_search`, `graph_path_search`, `graph_subgraph_search`) over stdio or bearer-authenticated loopback HTTP with a local status/configuration UI |
| Temporal deployment | PostgreSQL-backed local development stack; production deployment remains application/operator work |

## Why the package boundaries exist

Engineering RAG combines several failure domains: source authentication and pagination, hostile or malformed documents, model routing, tenant-aware persistence, retries, orchestration, and public interfaces. Keeping them separate provides three practical benefits:

- provider SDKs do not leak into domain contracts or orchestration;
- most behavior can be tested without live credentials or services;
- applications can choose providers independently for each capability family.

The package dependency direction is checked in CI. See [Architecture](../developers/architecture/README.md) for the exact graph.

## Intended use today

Use the repository to develop and test adapters, run the PostgreSQL-backed
Temporal ingestion stack, and execute opt-in real-system acceptance checks in a
controlled environment. Internet-facing production deployments still require
operator-owned identity policy, TLS/network controls, secret delivery,
backup/restore, resource limits, and alerting.

## Next steps

- [Installation](installation.md)
- [Quick Start](quick-start.md)
- [Configuration](../users/configuration/README.md)
- [Extending HarborRAG](../developers/extending/README.md)
