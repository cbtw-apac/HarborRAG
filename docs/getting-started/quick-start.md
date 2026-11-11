# Quick Start

In this tutorial you clone HarborRAG, start its local stack, ingest the repository's own
`docs/` folder, and ask a question about it. It takes about half an hour, most of it spent
waiting for the first container build.

HarborRAG reads documents from your sources, splits them into passages, and stores those
passages so you can search them and chat over them. Everything in this tutorial runs on your
machine, in Docker containers, except the language models, which you reach through a model
gateway.

## Before you start

Install these tools first. Each link goes to the tool's own installation guide.

| Tool | Why HarborRAG needs it | Check it with |
| --- | --- | --- |
| [Git](https://git-scm.com/downloads) | Clones the repository | `git --version` |
| [uv](https://docs.astral.sh/uv/getting-started/installation/) | Installs HarborRAG's Python packages. HarborRAG needs Python 3.12, and uv downloads it if your machine does not have it | `uv --version` |
| [Docker Engine with Compose 2.24 or later](https://docs.docker.com/get-started/get-docker/) | Runs HarborRAG's services in containers | `docker compose version` |
| OpenSSL | The setup script uses it to generate keys. macOS and most Linux distributions include it | `openssl version` |
| curl | Checks that the stack is ready | `curl --version` |

<details markdown="1"><summary>macOS</summary>

Install [Docker Desktop for Mac](https://docs.docker.com/desktop/setup/install/mac-install/),
which includes Compose, and start it before you continue. The setup script runs with the
Bash and sed that macOS ships.

</details>

<details markdown="1"><summary>Linux</summary>

Install [Docker Engine](https://docs.docker.com/engine/install/) with its Compose plugin, then
follow the [post-installation steps](https://docs.docker.com/engine/install/linux-postinstall/)
so that your user can run `docker` without `sudo`. The setup script calls `docker` directly.

</details>

<details markdown="1"><summary>Windows</summary>

HarborRAG runs on Linux and macOS only, so use WSL 2:
[install WSL](https://learn.microsoft.com/windows/wsl/install), turn on
[Docker Desktop's WSL 2 backend](https://docs.docker.com/desktop/features/wsl/), and run every
command in this tutorial inside your WSL distribution.

</details>

You also need a **model gateway**: a [LiteLLM proxy](https://docs.litellm.ai/docs/simple_proxy)
that serves three models, which HarborRAG calls with one URL and one key:

- a chat model, which writes answers;
- a Cohere Embed v4 model, which turns each passage into the 1536-dimension vector
  HarborRAG searches (the shipped configuration calls it as a Cohere model, so another
  embedding model will not work without changing that configuration);
- a reranking model, which reorders search results for chat.

[The shipped gateway catalog](../users/configuration/model-config.md#the-shipped-gateway-catalog)
explains how HarborRAG calls each one. If your team already runs a gateway, ask for its URL,
a key, and the names of those three models. Every model call goes through the gateway, so
ingesting documents and chatting both cost a little.

Plan for about 1 GB of disk for the Python packages and several GB for the container images.

## 1. Clone the repository and install the CLI

```bash
git clone https://github.com/cbtw-apac/HarborRAG.git
cd HarborRAG
uv sync --all-packages --extra production
```

`uv sync` creates a `.venv/` folder inside the checkout and installs every HarborRAG package
into it. The `production` extra adds the clients that the `harborrag` command needs to reach
the stack: Temporal, PostgreSQL, Qdrant, FalkorDB and MinIO. The environment takes about
800 MB.

Run every command in this tutorial from the `HarborRAG` folder. Check that the command works:

```bash
uv run harborrag --help
```

`uv run` runs a command inside `.venv/`. You should see the commands grouped by purpose:
`me`, `doctor`, `auth` and `config` under Operations; `chat` under Chat; and `projects`,
`source-types`, `connections`, `ingestions`, `failures` and `settings` under Resources.

## 2. Create the environment files

HarborRAG keeps its settings and credentials in an `env/` folder that Git ignores. Create it:

```bash
scripts/deployment/dev.sh bootstrap
```

`bootstrap` copies seven templates from `env-example/` into `env/`, makes them readable only
by you, and generates two secrets: `HARBORRAG_SECRETS_ENCRYPTION_KEY` in `env/.env.database`,
which encrypts the source credentials HarborRAG stores, and a bearer token for the MCP server
in `env/.env.mcp`. You should see a `Created env/...` line for each file, and among them:

```text
Generated HARBORRAG_SECRETS_ENCRYPTION_KEY in env/.env.database; back it up: stored connection secrets cannot be read without it.
Generated a protected MCP bearer token in env/.env.mcp.
```

Copy the key from `env/.env.database` somewhere safe.

Two settings have no safe default, so fill them in now.

**The model gateway.** Open `env/.env.models`. The five lines at the top ship as:

```dotenv
HARBORRAG_URL=https://gateway.example.com
HARBORRAG_API_KEY=REPLACE_WITH_GATEWAY_API_KEY
HARBORRAG_CHAT_MODEL=REPLACE_WITH_GATEWAY_CHAT_MODEL
HARBORRAG_EMBED_MODEL=REPLACE_WITH_GATEWAY_EMBEDDING_MODEL
HARBORRAG_RERANKER_MODEL=REPLACE_WITH_GATEWAY_RERANK_MODEL
```

Replace all five values. `HARBORRAG_URL` is the gateway's base URL without `/v1`, and the
three model values are names the gateway serves. All five are required, even the reranker:
`config/models/models.yaml` reads every one of them when it loads. Leave every other
`REPLACE_WITH_...` placeholder further down the file as it is; this tutorial does not use them.

Check the URL, the key and the model names before you go on, because a mistake here only
shows up later, as failed documents in step 5:

```bash
(set -a; . env/.env.models; curl -s -H "Authorization: Bearer $HARBORRAG_API_KEY" "$HARBORRAG_URL/v1/models")
```

The parentheses read the file's values in a subshell, so they do not stay set in your shell.
The response should list the three model names you entered.

**The control database.** The *control database* is the PostgreSQL database where HarborRAG
records its sources and ingestion runs. Open `env/.env.database` and remove the `#` in front
of the `HARBORRAG_CONTROL_DB_URL` line, so that it reads:

```dotenv
HARBORRAG_CONTROL_DB_URL="postgresql+asyncpg://${POSTGRES_USER}:${POSTGRES_PASSWORD}@127.0.0.1:${POSTGRES_PORT:-5432}/${POSTGRES_DB}"
```

This line points the `harborrag` command on your machine at the stack's database. Without it,
step 5 stops with
`Refusing to ingest or change connections from a repo checkout in the local sqlite control DB`.

The template passwords (`change-me`) are fine for this tutorial: the stack publishes its ports
on `127.0.0.1` only, so nothing outside your machine can reach it.

## 3. Start the local stack

```bash
scripts/deployment/dev.sh up
```

`up` starts, in order:

1. the data services: PostgreSQL, Qdrant (passage vectors), FalkorDB (the knowledge graph),
   Redis, and MinIO (a copy of every source file);
2. Temporal, the workflow engine that runs each ingestion as a durable workflow, with its web
   UI;
3. the ingestion workers, which do the work Temporal schedules;
4. the HarborRAG API.

The stack publishes its services on these ports of `127.0.0.1`: 5432 (PostgreSQL), 6333 and
6334 (Qdrant), 6379 and 3000 (FalkorDB), 6380 (Redis), 9000 and 9001 (MinIO), 7233 and 8080
(Temporal and its UI), and 8000 and 9465 (the API). If another program already uses one of
them, `up` fails; stop that program, or change the matching `*_PORT` value in `env/` before
you start.

The first run builds the worker and API images, which takes several minutes. When the API
answers, `up` prints, among its last lines:

```text
HarborRAG API is healthy at http://127.0.0.1:8000/healthz
```

At its first start the API imports `config/sources/connectors.yaml`, whose entries describe
*connections*: sources HarborRAG can ingest, together with their settings. Each entry lands
in the project `default`, under its YAML key. This tutorial uses the `harborrag-workspace`
entry, which reads this repository's `docs/` folder and needs no credentials.

## 4. Check everything is in place

Ask the API whether it can reach every service:

```bash
curl http://127.0.0.1:8000/readyz
```

You should see:

```json
{"status":"ready","checks":{"postgres":"ok","redis":"ok","temporal":"ok","qdrant":"ok","graph":"ok"}}
```

If a check is not `ok`, its name tells you which service the API cannot reach. `docker ps`
shows whether that service's container is running, and
[Troubleshooting](../users/troubleshooting/README.md) lists common fixes. The Temporal UI at
<http://localhost:8080> shows each ingestion run while it works.

List the connections the API imported:

```bash
uv run harborrag connections list
```

In a terminal you should see a table with one row per connection, among them:

```text
  connection_ref                name                  source_type   status   updated_at
 ────────────────────────────────────────────────────────────────────────────────────────────────────────
  default/harborrag-workspace   harborrag-workspace   local_file    active   2026-11-05T03:45:36.924071Z
```

`connection_ref` is how every command names a connection: the project, a slash, then the
connection, `PROJECT_ID/CONNECTION_ID`. The other connections in the file need credentials
you have not set, so they may show the status `error`; this tutorial does not use them.

## 5. Ingest the documentation folder

*Ingestion* reads a connection's documents, splits them into passages, and stores each passage
as a vector in Qdrant and as nodes in the knowledge graph. Start it for
`default/harborrag-workspace` and wait for it to finish:

```bash
uv run harborrag ingestions start default/harborrag-workspace --wait
```

The command submits the run to the Temporal server on `localhost:7233`, and `--wait` keeps it
attached until the run ends. Each passage is sent to your gateway's embedding model.

While the run works, the command prints a line whenever the run's status or counts change:
the run's ID, its status, and how many documents were `discovered`, `processed`, `skipped`
and `failed`. The last line shows `succeeded`, and the command exits with status 0. A
document that fails is counted under `failed`; the rest of the run continues. If the whole
run fails or is cancelled, the command exits with status 8.

Run the same command again and the unchanged documents are counted as `skipped`: HarborRAG
only processes what changed.

If every document fails, check the gateway values from step 2. For other errors, see
[Troubleshooting](../users/troubleshooting/README.md).

## 6. Ask a question

The `harborrag` command searches as `cli:` followed by your user name on this machine,
which has no permissions on individual sources, so by default it finds nothing. Let it read
everything ingested into the `DEFAULT` *tenant*, the isolated set of sources and data that
this tutorial uses. The ingestion workers use the same setting from `env/.env.temporal`,
which the command does not read:

```bash
export HARBORRAG_CORPUS_ACCESS_MODE=tenant_shared
export HARBORRAG_CORPUS_SHARED_TENANT_ID=DEFAULT
```

These exports last for the current shell. Ask the chat model for an answer grounded in the
passages that match your question. It calls your gateway, so it can cost a little:

```bash
uv run harborrag chat "Summarise what the HarborRAG documentation covers in three bullet points."
```

The answer cites its sources as `[1]`, `[2]` and so on, and below it lists each number with
the title of the document it points to. An answer with no citations means the search found
nothing: check that the two exports above ran in this shell.

That is the whole loop: change documents, run `ingestions start`, then `chat`.

## What you built

You ran HarborRAG's full local stack, ingested a folder through a durable Temporal workflow,
and chatted over it. Stop the stack with `scripts/deployment/dev.sh down`; add
`--volumes` to discard the data as well.

Next:

- [Running HarborRAG from a checkout](../developers/checkout-quick-start.md) - restart and
  rebuild single services, drive ingestion runs, and run the tests.
- [Connections](../users/connections.md) - add Jira, Confluence, SharePoint or GitHub sources.
- [MCP setup and integration](../users/detailed-guides/mcp-server/setup-and-integration.md) -
  search HarborRAG from an IDE or an agent.
- [CLI reference](../users/cli-reference/README.md) - every command and option.
- [Troubleshooting](../users/troubleshooting/README.md) - fixes for common failures.
