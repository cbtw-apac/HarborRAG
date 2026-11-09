# User Documentation

This section is for anyone who runs HarborRAG or builds on it: adding sources, running
ingestions, searching and chatting, and configuring the services. New to HarborRAG? Start
with the [Quick Start](../getting-started/quick-start.md), which takes you from a clone of the
repository to a first answer.

HarborRAG offers three ways in: the `harborrag` command, run from a repository checkout; an
HTTP API for chat, connections, ingestions and runtime settings; and an MCP server with
eleven read-only tools for IDEs and agents.
[Capability status](../getting-started/what-is-harborrag.md#capability-status) lists what
each one implements today.

## Pages in this section

- [Chat](chat/README.md) - ask a question over HTTP or with `harborrag chat` and get an
  answer that cites its sources.
  - [Chat models](chat/models.md) - which model answers a turn and what a caller may choose.
  - [Conversation memory](chat/memory.md) - what a turn remembers and who can read a session.
  - [Limits and accounting](chat/limits.md) - deadlines, rate limits, retries and token
    usage.
- [Connections](connections.md) - connect a Jira, Confluence, SharePoint, GitHub or local
  source, then start, follow and retry its ingestions.
- [Ingestion modes](ingestion-modes.md) - skip records already indexed (`incremental`) or
  process every record again (`force`).
- [Scheduled ingestion](scheduled-ingestion.md) - run a connection's ingestion on a
  timetable.
- [Python SDK](python-sdk/README.md) - ingest, search, walk the graph and chat from your own
  Python code.
- [CLI reference](cli-reference/README.md) - every `harborrag` command and option.
- [Configuration](configuration/README.md) - the `config/` files, the `env/` files and the
  settings you change while HarborRAG runs.
  - [Connector configuration](configuration/connector-config.md) - the sources in
    `config/sources/connectors.yaml` and each provider's options.
  - [Parser configuration](configuration/parser-config.md) - how PDFs, Office files and
    images become text, including OCR.
  - [Chunking configuration](configuration/chunking-config.md) - chunk sizes and per-source
    profiles.
  - [Graph build configuration](configuration/graph-build-config.md) - how the knowledge
    graph is built.
  - [Model configuration](configuration/model-config.md) - the chat, embedding and
    reranking models and the gateway they go through.
  - [Runtime settings](configuration/runtime-settings.md) - the six settings you change
    without a restart.
  - [Engine configuration](configuration/config-file-reference.md) - every `HARBORRAG_*`
    environment variable the services read.
  - [Tenant and workspace status](configuration/workspace-mode.md) - keep each team's data
    apart with tenants.
- [MCP tools](detailed-guides/mcp-server/README.md) - the tools HarborRAG offers to AI
  assistants, IDEs and agents.
  - [MCP setup and integration](detailed-guides/mcp-server/setup-and-integration.md) -
    start the server, connect a client and issue reader keys.
- [Troubleshooting](troubleshooting/README.md) - symptoms, causes and fixes.
- [Upgrading](upgrading.md) - the steps each release needs on an existing deployment.

## Going deeper

To use connectors, parsers, model clients and repositories directly from Python, read the
[`harborrag-adapters` README](../../packages/harborrag-adapters/README.md) and the family
README nearest the implementation. To add a new provider, see
[Extending HarborRAG](../developers/extending/README.md).
