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
| **Use HarborRAG on your own documents - start here** | [Quick Start](quick-start.md) (10 minutes, `pip install`, no clone) |
| Understand what it does before installing | [What is HarborRAG?](what-is-harborrag.md) |
| See every install option and extra | [Installation](installation.md) |
| Work on HarborRAG itself, or run the Temporal-based durable mode | [Running HarborRAG from a checkout](../developers/checkout-quick-start.md) |
| Use it as a Python library | [Python SDK](../users/python-sdk/README.md) |
| Connect an IDE or agent | [MCP Tools](../users/detailed-guides/mcp-server/README.md) |

## What a first run looks like

```bash
pip install "harborrag[local]"
harborrag init my-harbor && cd my-harbor    # answer a few questions
docker compose up -d                        # three small local services
harborrag doctor                            # all ✓
harborrag ingest run workspace              # index the docs/ folder
harborrag chat "What do these documents cover?"
```

`init` generates the encryption key and service passwords; the only value you supply is
your model provider's API key. If anything is missing, `harborrag doctor` names the exact
variable and the fix. The [Quick Start](quick-start.md) shows the expected output
of every step.

### In a repository checkout

Contributors work from the `env/` folder created by `scripts/deployment/dev.sh bootstrap`.
Two values there have no safe default:

- `HARBORRAG_SECRETS_ENCRYPTION_KEY` in `env/.env.database` - ships **empty**, and Docker
  Compose refuses to start until you set it (`openssl rand -hex 32`).
- The six `HARBOR_CHAT_*` and `HARBOR_EMBED_*` values in `env/.env.models` - the active
  model catalog expands references eagerly, so a missing embedding variable fails exactly
  as hard as a missing chat one.

[Running from a checkout, step 5](../developers/checkout-quick-start.md#5-create-the-env-folder) walks through both.

## Then

- [Configuration](../users/configuration/README.md) - point HarborRAG at your own
  connectors, parsers, and models
- [CLI Reference](../users/cli-reference/README.md) - every operator command
- [Architecture](../developers/architecture/README.md) - read this before contributing code
- [Troubleshooting](../users/troubleshooting/README.md) - when something does not work
