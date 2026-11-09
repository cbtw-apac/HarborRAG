# Getting Started

Start here to install HarborRAG and get a first answer from your own documents, or to
pick the guide that fits what you want to do.

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

## See what a first run looks like

```bash
pip install "harborrag[local]"
harborrag init my-harbor && cd my-harbor
docker compose up -d
harborrag doctor
harborrag ingest run workspace
harborrag chat "What do these documents cover?"
```

`init` asks a few questions and writes the project; `docker compose up -d` starts three
small local services; `doctor` checks the setup; `ingest run workspace` indexes the
`docs/` folder through the local-folder connection `init` created.

`init` generates the encryption key and service passwords; the only value you supply is
your model provider's API key. If anything is missing, `harborrag doctor` names the exact
variable and the fix. The [Quick Start](quick-start.md) shows the expected output
of every step.

### Set up a repository checkout

Contributors work from the `env/` folder created by `scripts/deployment/dev.sh bootstrap`.
`bootstrap` also generates `HARBORRAG_SECRETS_ENCRYPTION_KEY` in `env/.env.database`; back
it up, because stored connector secrets cannot be read without it. These values have no
safe default:

- The five model gateway values in `env/.env.models`: `HARBORRAG_URL`,
  `HARBORRAG_API_KEY`, `HARBORRAG_CHAT_MODEL`, `HARBORRAG_EMBED_MODEL` and
  `HARBORRAG_RERANKER_MODEL`. The active model catalog expands references eagerly, so a
  missing reranker variable fails exactly as hard as a missing chat one.
- The `HARBORRAG_CONTROL_DB_URL` line in `env/.env.database`, which ships commented out.
  Uncomment it so the host `harborrag` command uses the stack's Postgres; without it,
  `harborrag ingest` and `harborrag source` refuse to write from a checkout.

[Running from a checkout, step 5](../developers/checkout-quick-start.md#5-create-the-env-folder)
walks through them.

## Read next

- [Configuration](../users/configuration/README.md) - point HarborRAG at your own
  connectors, parsers, and models
- [CLI Reference](../users/cli-reference/README.md) - every operator command
- [Architecture](../developers/architecture/README.md) - read this before contributing code
- [Troubleshooting](../users/troubleshooting/README.md) - when something does not work
