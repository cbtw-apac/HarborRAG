# What is HarborRAG?

HarborRAG is a provider-agnostic Retrieval-Augmented Generation (RAG) framework for
engineering knowledge: it ingests documents from your sources, indexes them as vectors and as
a knowledge graph, and lets you search and chat over them through a CLI, an HTTP API and MCP
tools.
It uses ports and adapters, so source systems, document formats, model providers, persistence
backends and operator surfaces can change independently. This page explains what is built
today and why the code is split the way it is.

The first public alpha release is **HarborRAG 2.0.0 Alpha 1** (Python package version
`2.0.0a1`), dated August 27, 2026. The latest published release is **HarborRAG 2.0.0
Alpha 2** (`2.0.0a2`), dated October 26, 2026. These docs describe the repository's current
code, which has changed since 2.0.0a2; the [changelog](../../CHANGELOG.md) lists what changed
in each release and since the last one.

## Capability status

HarborRAG is under active alpha development. The distinction between implemented adapters and
the default application path matters:

| Layer | Status |
| --- | --- |
| Core domain and schemas | Implemented provider-neutral document, retrieval, model, storage, tenancy, security and telemetry contracts |
| Source connectors | Implemented for local files, GitHub, Confluence, Jira and SharePoint |
| Document parsers | Implemented for common text, structured, Office, image, ebook and PDF formats |
| Model adapters | Implemented chat, embedding and reranking clients with validation, routing, retries, caching, budgets and telemetry boundaries |
| Repository adapters | Implemented backends for Qdrant, FalkorDB, Redis, PostgreSQL, SQLite, S3, filesystem and memory |
| Engine and runtime | Postgres-authoritative ingestion, durable Temporal workflows, immutable artifacts, hybrid vector projection, graph projection, retrieval, chat orchestration, stored prompts and production dependency composition are implemented |
| CLI | Run from a repository checkout, calling the same operations as the HTTP API: projects, connections, Temporal-backed and inline ingestion runs, the failure inbox, runtime settings (`settings`, `config`) and one-shot chat, plus health checks (`doctor`) and MCP reader keys (`auth`); see the [CLI reference](../users/cli-reference/README.md) |
| HTTP API | FastAPI factory serving a public `/v1` contract (chat, connections, ingestions, failures, projects and runtime settings), root `/healthz` and `/readyz` probes, and Prometheus metrics on a separate ops port |
| MCP | Audited, policy-bounded FastMCP transport exposing eleven read-only evidence, source and graph tools over stdio or bearer-authenticated loopback HTTP, with a local status and configuration UI; see [MCP Tools](../users/detailed-guides/mcp-server/README.md) |
| Temporal deployment | PostgreSQL-backed local development stack; production deployment remains application and operator work |

## Why the package boundaries exist

Engineering RAG combines several failure domains: source authentication and pagination,
hostile or malformed documents, model routing, tenant-aware persistence, retries,
orchestration and public interfaces. Keeping them separate gives three practical benefits:

- provider SDKs do not leak into domain contracts or orchestration;
- most behaviour can be tested without live credentials or services;
- applications can choose providers independently for each capability family.

CI checks the package dependency direction. See
[Architecture](../developers/architecture/README.md) for the exact graph.

## Intended use today

Use the repository to develop and test adapters, run the PostgreSQL-backed Temporal ingestion
stack, and run opt-in real-system acceptance checks in a controlled environment.
Internet-facing production deployments still need operator-owned identity policy, TLS and
network controls, secret delivery, backup and restore, resource limits and alerting; see
[Deployment](../developers/deployment/README.md#what-a-production-deployment-still-needs).

## Next steps

- [Quick Start](quick-start.md) - clone HarborRAG, run it locally and ask a first question.
- [Installation](installation.md) - install options, extras and parser backends.
- [Configuration](../users/configuration/README.md) - point HarborRAG at your own sources and
  models.
- [Extending HarborRAG](../developers/extending/README.md) - add a connector, parser or
  backend.
