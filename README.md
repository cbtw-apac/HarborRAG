# HarborRAG

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="website/assets/logos/HarborRAG-logo-dark.png">
    <source media="(prefers-color-scheme: light)" srcset="website/assets/logos/HarborRAG-logo-light.png">
    <img src="website/assets/logos/HarborRAG-logo-light.png" alt="HarborRAG" width="360">
  </picture>
</p>

<h3 align="center">A modular, provider-agnostic RAG framework for engineering knowledge</h3>

<p align="center">
  Ingest from the systems your team already uses, and retrieve answers that can only cite
  the document version that is actually current.
</p>

<p align="center">
  <a href="https://www.python.org/downloads/"><img alt="Python 3.12+" src="https://img.shields.io/badge/python-3.12%2B-blue"></a>
  <a href="https://cbtw-apac.github.io/HarborRAG/coverage/"><img alt="Test coverage" src="https://img.shields.io/badge/coverage-view%20reports-blue"></a>
  <a href="https://www.apache.org/licenses/LICENSE-2.0"><img alt="License: Apache 2.0" src="https://img.shields.io/badge/License-Apache%202.0-blue.svg"></a>
</p>

---

HarborRAG is a retrieval-augmented generation (RAG) framework: it reads documents from the
systems your team already uses, splits them into passages, indexes those passages as vectors
and as a knowledge graph, and lets people and AI assistants search them and get answers that
cite the passages they came from. You run it from a clone of this repository, with its
services in Docker containers on your machine and the language models behind a model gateway.

> **Status** HarborRAG is alpha software, built for local development, integration testing
> and controlled single-host staging; staging is supported once the manual release gate, the
> [deployed ingestion smoke](packages/harborrag-runtime/tests/runtime_ingestion/smoke/README.md),
> passes against the deployed candidate. These docs describe the repository's current code;
> the latest published release, `2.0.0a2`, is older. A production deployment still needs
> operator-owned security and operations; see
> [What a production deployment still needs](docs/developers/deployment/README.md#what-a-production-deployment-still-needs).

## Get started

Follow the [Quick Start](docs/getting-started/quick-start.md). In about half an hour it takes
you from `git clone` to an answer about this repository's own documentation: you clone the
repository, fill in two settings, start the local stack, ingest a folder and ask a question.

You need Git, uv, Docker Engine with Compose v2, and a model gateway (a LiteLLM proxy) that
serves a chat, an embedding and a reranking model. The Quick Start links each tool's own
installation guide. HarborRAG runs on Linux and macOS, and on Windows inside WSL 2.

**If `scripts/deployment/dev.sh up` stops with**
`HARBORRAG_SECRETS_ENCRYPTION_KEY is blank in env/.env.database, but compose_pg_data already holds a database; restore the key it was written with.`,
the key that encrypts stored connection secrets is empty while a database from an earlier
run still exists. `dev.sh` generates a key only while no database volume exists, because a
new key cannot read secrets stored under the old one. Put the key you backed up back into
`HARBORRAG_SECRETS_ENCRYPTION_KEY` in `env/.env.database`. When you set up a fresh database
without `dev.sh`, set the key yourself; this command prints a new one:

```bash
openssl rand -hex 32
```

After the Quick Start:

- [Running HarborRAG from a checkout](docs/developers/checkout-quick-start.md) - start and
  rebuild one service at a time, drive ingestion runs and run the tests.
- [Connections](docs/users/connections.md) - add Jira, Confluence, SharePoint or GitHub
  sources.
- [MCP setup and integration](docs/users/detailed-guides/mcp-server/setup-and-integration.md) -
  search HarborRAG from Claude Desktop, Cursor, VS Code or another MCP client. On Windows,
  the MCP container also needs Docker Desktop host networking; see the
  [`harborrag-mcp-server` README](packages/harborrag-mcp-server/README.md#docker-desktop-on-windows).
- [Python SDK](docs/users/python-sdk/README.md) - ingest, search and chat from your own
  Python code.
- [Installation](docs/getting-started/installation.md) - reference for later: pip installs,
  extras and parser backends.

<details markdown="1">
<summary>Local stack notes: environment files, container status, worker replicas</summary>

**Environment file paths.** `scripts/deployment/dev.sh` reads each `env/` file from a path
you can override, so the files can live outside the checkout: `DATABASE_ENV_FILE`,
`TEMPORAL_ENV_FILE`, `CONNECTOR_ENV_FILE`, `PARSER_ENV_FILE`, `MODEL_ENV_FILE`,
`API_ENV_FILE` and `MCP_ENV_FILE`. `scripts/deployment/dev.sh --help` lists them, and
[MCP setup](docs/users/detailed-guides/mcp-server/setup-and-integration.md#override-launcher-settings-with-environment-variables)
lists the ones the MCP wrapper accepts.

```bash
DATABASE_ENV_FILE=/etc/harborrag/database.env \
MODEL_ENV_FILE=/etc/harborrag/models.env \
  scripts/deployment/dev.sh up
```

**Creating `env/` without `dev.sh`.** `dev.sh bootstrap` copies each
`env-example/.env.<name>.example` template to `env/.env.<name>`, makes the files readable only
by you (mode `0600`), and generates two secrets. By hand, generate
`HARBORRAG_SECRETS_ENCRYPTION_KEY` in `env/.env.database` and `HARBORRAG_MCP_BEARER_TOKEN` in
`env/.env.mcp`, which must be at least 32 bytes, each with `openssl rand -hex 32`.

**Container status.** List the data services and the Temporal containers:

```bash
docker compose \
  --env-file env/.env.database \
  --file deploy/compose/docker-compose.database.yml \
  ps

docker compose \
  --env-file env/.env.database \
  --env-file env/.env.temporal \
  --file deploy/compose/docker-compose.temporal.yml \
  --profile worker \
  ps -a
```

`postgres`, `temporal`, `temporal-ui` and every `temporal-worker` replica should be running.
`temporal-schema` and `temporal-namespace` are one-shot jobs and should show `Exited (0)`.

**Worker capacity.** Concurrency, retry budgets and worker counts are deployment policy: tune
them for source quotas, model latency, document size and the host's resources. Worker
capacity, task queues, retries and timeouts live in `config/temporal.yaml`
([Temporal configuration](deploy/temporal/README.md#settings-reference)); the number of worker
replicas is `HARBORRAG_TEMPORAL_WORKER_REPLICAS` in `env/.env.temporal`.

</details>

## Key features

- **Your existing sources.** Local files, GitHub, Confluence, Jira and SharePoint, including
  issue comments and attachments; see [Connections](docs/users/connections.md).
- **Common document formats.** Text, Markdown, HTML, JSON, CSV and Excel, Word, ODT, EPUB,
  PowerPoint, images and PDF. PDFs go through PyMuPDF, Docling, LiteParse, MinerU or
  PaddleOCR, with optional OCR for scanned pages; see
  [Parser configuration](docs/users/configuration/parser-config.md).
- **Answers cite only the current version.** PostgreSQL holds the authoritative document and
  version state; Qdrant (vectors) and FalkorDB (knowledge graph) hold projections that can be
  rebuilt. A version becomes searchable only after its projections are verified, and every
  search result is checked against the active version, so a superseded version is never
  cited, even during a reindex; see [Data lifecycle](docs/developers/architecture/data-lifecycle.md).
- **Re-ingesting skips what did not change.** A document whose source, processing settings,
  content and metadata are unchanged is reported as `unchanged` and is not parsed or embedded
  again; see [Ingestion modes](docs/users/ingestion-modes.md).
- **Rebuild the indexes without the source.** Immutable artifacts in object storage let the
  `harborrag.reindex` workflow rebuild vectors and graph without calling a connector; see
  [Projection rebuild](docs/developers/architecture/projection-rebuild.md).
- **Vectors and a knowledge graph.** Retrieval runs dense, sparse or hybrid search with
  optional graph context, chat can rerank the results, and agents get graph tools that walk
  from a passage to related entities and documents.
- **Durable ingestion.** Ingestion runs as [Temporal](https://docs.temporal.io/) workflows
  that survive worker restarts and can be paused, resumed or cancelled. A document that fails
  does not stop the run, and you can retry failed documents on their own. Search never goes
  through Temporal. See
  [Runtime reliability](docs/developers/architecture/runtime-reliability.md).
- **Three ways in.** The `harborrag` CLI, an HTTP API for chat, connections and ingestion, and
  an MCP server with eleven read-only, audited tools for IDEs and agents; see the
  [CLI reference](docs/users/cli-reference/README.md), [Chat](docs/users/chat/README.md) and
  [MCP tools](docs/users/detailed-guides/mcp-server/README.md).
- **Nothing hardwired.** Connectors, parsers, model providers and storage sit behind
  provider-neutral contracts across eight packages with a checked dependency direction; see
  [Architecture](docs/developers/architecture/README.md).

[What is HarborRAG?](docs/getting-started/what-is-harborrag.md) lists what is implemented
today, layer by layer.

## Documentation

| Start here | |
| --- | --- |
| [Quick Start](docs/getting-started/quick-start.md) | From `git clone` to a first answer |
| [What is HarborRAG?](docs/getting-started/what-is-harborrag.md) | What is built today and why the code is split into packages |
| [Installation](docs/getting-started/installation.md) | Install options, extras and parser backends |
| [Documentation index](docs/TOC.md) | Every page, in order |

| Using it | |
| --- | --- |
| [Connections](docs/users/connections.md) | Add and manage sources over HTTP and the CLI |
| [Configuration](docs/users/configuration/README.md) | Connectors, parsers, chunking, models, runtime settings, tenants |
| [CLI reference](docs/users/cli-reference/README.md) | Every command and option |
| [Python SDK](docs/users/python-sdk/README.md) | Use HarborRAG as a library |
| [Chat](docs/users/chat/README.md) | Retrieval-grounded chat over HTTP and the CLI |
| [MCP tools](docs/users/detailed-guides/mcp-server/README.md) | Tools for IDEs and agents |
| [Troubleshooting](docs/users/troubleshooting/README.md) | Fixes for common failures |
| [Upgrading](docs/users/upgrading.md) | The steps each release needs on an existing deployment |

| Building on it | |
| --- | --- |
| [Running HarborRAG from a checkout](docs/developers/checkout-quick-start.md) | Develop against the local stack |
| [Architecture](docs/developers/architecture/README.md) | Packages, boundaries and invariants |
| [Data lifecycle](docs/developers/architecture/data-lifecycle.md) | Authority, evidence, projections, publication |
| [Extending HarborRAG](docs/developers/extending/README.md) | New connectors, parsers, models and storage backends |
| [Testing](docs/developers/testing/README.md) | Layout, markers and real-system smoke checks |
| [Deployment](docs/developers/deployment/README.md) | Local stacks, images and the production boundary |
| [Control Plane API](packages/harborrag-app/src/harborrag_app/api/README.md) | HTTP surface internals |

## Contributing

Contributions are welcome. [CONTRIBUTING.md](CONTRIBUTING.md) covers the development setup,
package ownership, the quality gates and pull requests. Maintainers publish releases as
described in [Release process](docs/developers/release-process.md).

## Community and support

- **Questions and ideas** - [GitHub Discussions](https://github.com/cbtw-apac/HarborRAG/discussions)
- **Bugs and feature requests** - [GitHub Issues](https://github.com/cbtw-apac/HarborRAG/issues)
- **Security** - do not open a public issue; follow [SECURITY.md](SECURITY.md)

## License

HarborRAG is licensed under the [Apache License 2.0](LICENSE). Every published package
declares `Apache-2.0` and ships a copy of the license in its distribution.

<p align="center">
  <sub>A project of <strong>CBTW</strong></sub>
</p>
