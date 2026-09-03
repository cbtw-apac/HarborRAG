# User Documentation

New here? Start with the [Quick Start](../getting-started/quick-start.md) - Path A takes
five minutes and needs no Docker or credentials.

## By task

| You want to… | Go here |
| --- | --- |
| Point HarborRAG at your own sources | [Configuration](configuration/README.md) |
| Run ingestion and retrieval from a terminal | [CLI Reference](cli-reference/README.md) |
| Use it as a Python library | [Python SDK](python-sdk/README.md) |
| Ask questions over your content | [Chat](chat/README.md) |
| Connect an IDE or agent | [MCP Tools](detailed-guides/mcp-server/README.md) |
| Understand re-ingestion and retries | [Ingestion Modes](ingestion-modes.md) |
| Fix something that broke | [Troubleshooting](troubleshooting/README.md) |

## Configuration in detail

- [Connector configuration](configuration/connector-config.md) - where your documents come from
- [Parser configuration](configuration/parser-config.md) - how they are read, including PDF and OCR backends
- [Model configuration](configuration/model-config.md) - chat, embedding, and reranking providers
- [Engine configuration](configuration/config-file-reference.md) - runtime and repository settings
- [Tenant and workspace status](configuration/workspace-mode.md) - multi-tenant scoping

## What HarborRAG exposes today

| Surface | What it does |
| --- | --- |
| CLI | `doctor`, `chat`, `retrieve`, and the `ingest` subgroup, all Temporal-backed |
| HTTP API | `/v1/...` public contract - ingestion, retrieval, chat, agent, admin; `/api/v1/...` operational routes |
| MCP | five read-only retrieval tools over stdio or bearer-authenticated loopback HTTP, plus a browser playground |

The MCP server implements stdio plus bearer-authenticated, loopback-only Streamable HTTP.
TLS, remote exposure, and production token verification remain deployment-owned. The
[project status](../getting-started/what-is-harborrag.md#capability-status) records the
current boundary.

## Going deeper

For direct Python use of connectors, parsers, model clients, and repositories, read
[`packages/harborrag-adapters/README.md`](../../packages/harborrag-adapters/README.md) and
the family README nearest the implementation. To add a new provider, see
[Extending HarborRAG](../developers/extending/README.md).
