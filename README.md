<h1 align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="website/assets/logos/HarborRAG-logo-dark.png">
    <source media="(prefers-color-scheme: light)" srcset="website/assets/logos/HarborRAG-logo-light.png">
    <img src="website/assets/logos/HarborRAG-logo-light.png" alt="HarborRAG" width="360">
  </picture>
</h1>

<p align="center"><strong>Ask questions across your engineering docs and tickets. Trace each citation back to its source.</strong></p>

HarborRAG is an open-source retrieval-augmented generation (RAG) framework for teams whose
knowledge is spread across GitHub, Confluence, Jira, SharePoint, and local files. It indexes
that material for search by meaning and builds a knowledge graph you can explore from your
own application or AI assistant.

**[Run the Quick Start](docs/getting-started/quick-start.md)** ·
[Connect an MCP client](docs/users/detailed-guides/mcp-server/clients.md) ·
[Browse the documentation](docs/TOC.md)

## Get your first cited answer

The Quick Start runs HarborRAG locally in Docker and indexes this repository’s documentation.
You can try it before connecting any team accounts. Allow about 30 minutes for the first run.
You’ll need Git, uv, Docker Compose, and access to chat and embedding models.
[Check the prerequisites and start the tutorial](docs/getting-started/quick-start.md#before-you-start).

Once the tutorial has indexed the docs, ask a question from the repository root.

```bash
uv run harborrag chat "How do I ingest a local folder?"
```

Look for numbered citations such as `[1]` in the answer. The CLI lists the corresponding
document titles below it so you can check where the instructions came from.

## What you can build

- **A searchable engineering library.** Index project files, wiki pages, and issues together.
  Schedule ingestion to pick up edits and skip unchanged documents. [Connections](docs/users/connections.md) · [Scheduled ingestion](docs/users/scheduled-ingestion.md)
- **An assistant that cites your docs.** Give an IDE or assistant read-only search tools through
  the Model Context Protocol (MCP). It can find a passage, fetch the original text, and read the
  surrounding section before citing it. [MCP tools](docs/users/detailed-guides/mcp-server/README.md)
- **An application that explores relationships.** Use vector search to find relevant passages,
  then query the knowledge graph for related entities and the paths between them.
  [Graph queries in Python](docs/users/python-sdk/README.md#graph) · [Graph search tools](docs/users/detailed-guides/mcp-server/README.md#tools)

## Find your next guide

| I want to… | Guide |
| --- | --- |
| Ask questions from an application | [Chat HTTP API](docs/users/chat/README.md) |
| Use my own model provider | [Model configuration](docs/users/configuration/model-config.md) |
| Fix a setup or indexing problem | [Troubleshooting](docs/users/troubleshooting/README.md) |
| Run or upgrade a deployment | [Deployment](docs/developers/deployment/README.md) · [Upgrading](docs/users/upgrading.md) |

**HarborRAG is in alpha.** This README describes the current checkout.
See the [release notes](CHANGELOG.md) for changes included in your version.

## Contributing and support

For local development and pull requests, start with [Contributing](CONTRIBUTING.md).
Ask questions in [GitHub Discussions](https://github.com/cbtw-apac/HarborRAG/discussions),
report bugs in [GitHub Issues](https://github.com/cbtw-apac/HarborRAG/issues), or follow the
[security policy](SECURITY.md) to report a vulnerability privately.

HarborRAG is a CBTW project, licensed under [Apache 2.0](LICENSE).
