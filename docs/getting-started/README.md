# Getting Started

HarborRAG is a modular, provider-agnostic RAG framework for engineering knowledge. It
ingests from the systems your team already uses and resolves retrieval against the
authoritative *current* document version, so a superseded version cannot be cited even
while a reindex is in flight.

It is alpha software. The adapter layer contains real connectors, parsers, model clients,
and storage providers; the runtime composes ingestion, retrieval, and chat behind the HTTP
and CLI surfaces, and retrieval behind the MCP surface.

## Pick your path

| You want to… | Go here |
| --- | --- |
| Understand what it does before installing | [What is HarborRAG?](what-is-harborrag.md) |
| Get it running | [Quick Start](quick-start.md) |
| Add it to an existing project | [Installation](installation.md) |
| Use it as a Python library | [Python SDK](../users/python-sdk/README.md) |
| Connect an IDE or agent | [MCP Tools](../users/detailed-guides/mcp-server/README.md) |

The [Quick Start](quick-start.md) has two paths. Path A takes about five minutes and needs
no Docker, no credentials, and no services - it confirms the install and shows the parser
working. Path B brings up the full local stack for real ingestion, retrieval, and chat.

## The one thing that trips people up

Every credential lives in an ignored `env/` folder that you create once with
`scripts/deployment/dev.sh bootstrap`. Two values there have no safe default:

- `HARBORRAG_SECRETS_ENCRYPTION_KEY` in `env/.env.database` - ships **empty**, and Docker
  Compose refuses to start until you set it (`openssl rand -hex 32`).
- The six `HARBOR_CHAT_*` and `HARBOR_EMBED_*` values in `env/.env.models` - the active
  model catalog expands references eagerly, so a missing embedding variable fails exactly
  as hard as a missing chat one.

[Quick Start step 5](quick-start.md#5-create-the-env-folder) walks through both.

## Then

- [Configuration](../users/configuration/README.md) - point HarborRAG at your own
  connectors, parsers, and models
- [CLI Reference](../users/cli-reference/README.md) - every operator command
- [Architecture](../developers/architecture/README.md) - read this before contributing code
- [Troubleshooting](../users/troubleshooting/README.md) - when something does not work
