# Getting Started

This section is for anyone new to HarborRAG. Start with the [Quick Start](quick-start.md): one
tutorial that takes you from a clone of the repository to a first answer about your
documents.

HarborRAG is a retrieval-augmented generation (RAG) framework for engineering knowledge. It
ingests documents from the systems your team already uses, and its answers cite only the
current version of each document, so a superseded version is never cited, even while the
indexes are being rebuilt. It is alpha software.

## Pages in this section

- [What is HarborRAG?](what-is-harborrag.md) - what HarborRAG does, what is built today and
  why the code is split into packages.
- [Installation](installation.md) - reference for later: uv and pip installs, extras, parser
  backends and platform notes. The Quick Start installs everything it needs.
- [Quick Start](quick-start.md) - **start here**: clone the repository, start the local stack
  in Docker, ingest the repository's documentation and ask a question, in about half an hour.

## What the Quick Start asks you to set

The Quick Start creates an `env/` folder of settings with `scripts/deployment/dev.sh bootstrap`,
which also generates the key that encrypts stored connection secrets; back that key up. Two
settings have no safe default: the five model gateway values in `env/.env.models`, and the
`HARBORRAG_CONTROL_DB_URL` line in `env/.env.database`, which ships commented out.
[Quick Start step 2](quick-start.md#2-create-the-environment-files) explains each one.

## Other paths

| You want to… | Go here |
| --- | --- |
| Work on HarborRAG's code after the Quick Start | [Running HarborRAG from a checkout](../developers/checkout-quick-start.md) |
| Use HarborRAG from your own Python code | [Python SDK](../users/python-sdk/README.md) |
| Connect an IDE or an AI assistant | [MCP setup and integration](../users/detailed-guides/mcp-server/setup-and-integration.md) |
| Add your own sources | [Connections](../users/connections.md) |

## Read next

- [Configuration](../users/configuration/README.md) - point HarborRAG at your own
  connectors, parsers and models.
- [CLI Reference](../users/cli-reference/README.md) - every command and option.
- [Architecture](../developers/architecture/README.md) - read this before contributing code.
- [Troubleshooting](../users/troubleshooting/README.md) - fixes for common failures.
