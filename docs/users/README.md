# User Documentation

Start with the [Quick Start](../getting-started/quick-start.md). HarborRAG currently exposes a small local CLI and in-process MCP facade, while the adapter packages provide the broader implemented functionality.

- [CLI Reference](cli-reference/README.md) — runnable and stubbed commands.
- [Configuration](configuration/README.md) — connector, parser, model, engine, and tenant configuration.
- [MCP Mock Tools](detailed-guides/mcp-server/README.md) — in-process tool listing and calls.
- [Troubleshooting](troubleshooting/README.md) — setup, configuration, provider, and quality-gate failures.

For direct Python usage of connectors, parsers, model clients, and repositories, also consult `packages/harborrag-adapters/README.md` and the family README nearest the implementation.

The FastAPI routes, external MCP transport, full production pipeline, and durable workflow surfaces remain incomplete. The [project status](../getting-started/what-is-harborrag.md#capability-status) records the current boundary.
