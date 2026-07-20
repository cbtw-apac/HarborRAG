# What is HarborRAG?

HarborRAG is a modular, provider-agnostic Retrieval-Augmented Generation (RAG) framework for engineering knowledge. It is built around a ports-and-adapters (hexagonal) architecture so that connectors, parsers, model providers, and storage repositories can be implemented and swapped independently, without leaking provider SDKs into orchestration or business logic.

## Status: active framework development

HarborRAG is not yet a finished product. The repository currently provides:

- stable core contracts, domain models, and protocol ports (`harborrag-core`);
- provider contracts plus real connectors, parsers, model adapters, and storage repositories (`harborrag-adapters`);
- ingestion and retrieval orchestration skeletons (`harborrag-engine`);
- runtime composition, local job state, and scheduling scaffolding (`harborrag-runtime`);
- a CLI/API package boundary with a mock application service (`harborrag-app`);
- an MCP tool facade with policy and audit boundaries, backed by mock tools (`harborrag-mcp`);
- a meta-package public facade (`harborrag`).

The repository layer includes Qdrant, FalkorDB, Redis, S3, PostgreSQL, SQLite,
filesystem, and memory providers across vector, graph, cache, object, database,
and workflow-state families. The application and runtime composition remains an
evolving framework rather than a finished product.

## Why a ports-and-adapters framework?

Engineering RAG systems accumulate risk when provider SDKs, orchestration logic, and API/CLI surfaces are tangled together:

- swapping a vector database or an embedding provider becomes a rewrite instead of a new adapter;
- tests require live credentials because there is no seam to mock;
- CLI, HTTP, and MCP tool code duplicate business rules instead of sharing one service layer;
- dependency direction erodes until every package imports every other package.

HarborRAG addresses this by giving every capability family a provider-neutral
contract, isolating concrete providers behind adapter packages, keeping fakes in
tests, and enforcing a one-directional dependency graph between packages:

```text
harborrag-core      (no dependencies on other HarborRAG packages)
harborrag-adapters  -> core
harborrag-engine    -> core, adapters
harborrag-runtime   -> core, adapters, engine
harborrag-app       -> core, engine, runtime
harborrag-mcp       -> core, engine, runtime
harborrag           -> any package (public facade)
```

This direction is enforced automatically by `scripts/check_dependency_direction.py` and `make deps-check`.

## Where to go next

- [Quick Start](quick-start.md) — install the workspace and run the deterministic mock pipeline end to end.
- [Architecture Overview](../developers/architecture/README.md) — package map, dependency rules, and a tour of core contracts.
- [Extending HarborRAG](../developers/extending/README.md) — how to implement a real connector, parser, model, or repository.
