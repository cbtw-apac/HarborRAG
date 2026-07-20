# Getting Started with HarborRAG

HarborRAG is a modular, provider-agnostic RAG framework under active development. Stable contracts now have real connector, parser, model, and storage implementations, including Redis, FalkorDB, PostgreSQL, Qdrant, SQLite, S3, filesystem, and memory repositories; the end-user orchestration surfaces are still evolving.

## Start path

1. Understand the project: [What is HarborRAG?](what-is-harborrag.md)
2. Install the workspace and run the mock pipeline: [Quick Start](quick-start.md)
3. Platform-specific notes and troubleshooting: [Installation](installation.md)

## After getting started

- [Architecture Overview](../developers/architecture/README.md) — package map, dependency direction, core contracts.
- [Extending HarborRAG](../developers/extending/README.md) — implement a real connector, parser, model, or repository.
- [CLI Reference](../users/cli-reference/README.md) — the `harbor` command today and what's stubbed for later.
- [MCP Mock Tools](../users/detailed-guides/mcp-server/README.md) — the audited agent-tool facade.
