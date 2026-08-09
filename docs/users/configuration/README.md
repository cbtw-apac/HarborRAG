# Configuration

HarborRAG currently has three independent configuration paths:

1. [Connector Configuration](connector-config.md) — versioned YAML loaded by `harborrag-runtime`.
2. [Parser Configuration](parser-config.md) — versioned YAML loaded by `harborrag-runtime`.
3. [Model Configuration](model-config.md) — YAML or JSON loaded by the chat, embedding, and reranking clients in `harborrag-adapters`.

[Engine Configuration](config-file-reference.md) documents the small code-constructed engine dataclasses. [Tenant and Workspace Status](workspace-mode.md) explains tenant-aware repository context and what is not yet available as a workspace feature.

For the composed chat surfaces and stored prompt catalog, see
[Chat](../chat/README.md). For MCP tool defaults, limits, tenant overrides, and
the authenticated configuration UI, see
[MCP Setup and Integration](../detailed-guides/mcp-server/setup-and-integration.md).

The checked-in `*.example.yaml` and `.env.*.example` files are references, not automatically loaded runtime files. Copy them for an environment or pass their paths explicitly. HarborRAG does not automatically load dotenv files.
