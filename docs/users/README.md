# User Documentation

Start with the [Quick Start](../getting-started/quick-start.md). HarborRAG
currently exposes a Temporal-backed ingestion control plane, authenticated
retrieval and chat routes, one-shot CLI commands, and local MCP stdio or
authenticated loopback HTTP transports.

- [Chat](chat/README.md) — HTTP, CLI, MCP, prompts, model setup, and safety behavior.
- [CLI Reference](cli-reference/README.md) — runnable workflow commands.
- [Ingestion Modes](ingestion-modes.md) — incremental admission, forced evaluation, retries, and reindexing.
- [Configuration](configuration/README.md) — connector, parser, model, engine, and tenant configuration.
- [MCP Tools](detailed-guides/mcp-server/README.md) — retrieval and chat tools over stdio, loopback HTTP, or the browser playground.
- [Troubleshooting](troubleshooting/README.md) — setup, configuration, provider, and quality-gate failures.

For direct Python usage of connectors, parsers, model clients, and repositories, also consult `packages/harborrag-adapters/README.md` and the family README nearest the implementation.

The MCP server implements stdio plus bearer-authenticated, loopback-only
Streamable HTTP with a local status/configuration UI. TLS, remote exposure, and
production token verification remain deployment-owned.
The [project status](../getting-started/what-is-harborrag.md#capability-status)
records the current boundary.
