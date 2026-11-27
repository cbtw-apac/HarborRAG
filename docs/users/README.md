# User Documentation

Use these guides to connect your own sources, index them and ask questions about
what they contain. If you're setting up HarborRAG for the first time, start with the
[Quick Start](../getting-started/quick-start.md) and get a cited answer from the example
documents.

## Pages in this section

| I want to… | Start here |
| --- | --- |
| Add a Jira, Confluence, SharePoint, GitHub or local source | [Connections](connections.md) |
| Index a source or follow a run | [Start and control ingestions](connections.md#start-and-control-ingestions), [ingestion modes](ingestion-modes.md) |
| Keep my index up to date automatically | [Scheduled ingestion](scheduled-ingestion.md) |
| Ask a question and see its sources | [Chat](chat/README.md) |
| Search from an IDE or AI assistant | [MCP setup](detailed-guides/mcp-server/setup-and-integration.md), [connect your MCP client](detailed-guides/mcp-server/clients.md) |
| Integrate HarborRAG into a Python application | [Python SDK](python-sdk/README.md) |
| Find a terminal command or flag | [CLI reference](cli-reference/README.md) |
| Change how HarborRAG parses, indexes or searches | [Configuration](configuration/README.md), [runtime settings](configuration/runtime-settings.md) |
| Decide who can access indexed documents | [Tenant access](configuration/workspace-mode.md#read-access-inside-a-tenant) |
| Fix an error or update a deployment | [Troubleshooting](troubleshooting/README.md), [upgrading](upgrading.md) |

## Going deeper

Browse the [documentation index](../TOC.md) for the full list of guides. See
[Extending HarborRAG](../developers/extending/README.md) to add a provider or the
[`harborrag-adapters` README](../../packages/harborrag-adapters/README.md) to use an
adapter directly from Python.
