# Quick Start

Get a first answer with citations from HarborRAG in about half an hour. You'll run
the services locally in Docker, index the repository's `docs/` folder and ask a
question about it. Indexing and chat call a model provider, which may charge for use.

## Before you start

Have an API key that can call both a chat model and a model that produces
1536-dimension embeddings. The default endpoint is OpenAI and the default embedding
model is `text-embedding-3-small`. You can use another OpenAI-compatible endpoint and
its model names. Allow about 1 GB for the Python packages and several GB for Docker images.

| Tool | Why HarborRAG needs it | Check it with |
| --- | --- | --- |
| [Git](https://git-scm.com/downloads) | Clones the repository | `git --version` |
| [uv](https://docs.astral.sh/uv/getting-started/installation/) | Installs HarborRAG's Python packages. HarborRAG needs Python 3.12, and uv downloads it if your machine does not have it | `uv --version` |
| [Docker Engine with Compose 2.24 or later](https://docs.docker.com/get-started/get-docker/) | Runs HarborRAG's services in containers | `docker compose version` |
| OpenSSL | The setup script uses it to generate keys. macOS and most Linux distributions include it | `openssl version` |
| curl | Checks that the stack is ready | `curl --version` |

<details markdown="1">
<summary>macOS</summary>

Install [Docker Desktop for Mac](https://docs.docker.com/desktop/setup/install/mac-install/),
which includes Compose, and start it before you continue. The setup script runs with the
Bash and sed that macOS ships.

</details>

<details markdown="1">
<summary>Linux</summary>

Install [Docker Engine](https://docs.docker.com/engine/install/) with its Compose plugin, then
follow the [post-installation steps](https://docs.docker.com/engine/install/linux-postinstall/)
so that your user can run `docker` without `sudo`. The setup script calls `docker` directly.

</details>

<details markdown="1">
<summary>Windows</summary>

HarborRAG runs on Linux and macOS only, so use WSL 2:
[install WSL](https://learn.microsoft.com/windows/wsl/install), turn on
[Docker Desktop's WSL 2 backend](https://docs.docker.com/desktop/features/wsl/), and run every
command in this tutorial inside your WSL distribution.

</details>

## 1. Clone the repository and install the CLI

```bash
git clone https://github.com/cbtw-apac/HarborRAG.git
cd HarborRAG
uv sync --all-packages --extra production
```

`uv sync` installs HarborRAG and the clients the CLI needs to reach the local stack.
Run the rest of the tutorial from the `HarborRAG` folder. Check that the CLI works.

```bash
uv run harborrag --help
```

The help lists commands including `connections`, `ingestions` and `chat`.

## 2. Create the environment files

Create the local settings and credentials in the Git-ignored `env/` folder.

```bash
scripts/deployment/dev.sh bootstrap
```

On a fresh checkout, `bootstrap` reports each new file with a `Created env/...` line.
Back up `HARBORRAG_SECRETS_ENCRYPTION_KEY` from `env/.env.database` in a safe place.
You'll need it to read stored connection credentials after a database restore.

The script restricts the files to your user and generates the read-only account
passwords and bearer token used by the [MCP server](../users/detailed-guides/mcp-server/setup-and-integration.md).

### Configure the model endpoint

Open `env/.env.models` and locate these four lines.

```dotenv
HARBORRAG_CHAT_MODEL=REPLACE_WITH_CHAT_MODEL
HARBORRAG_CHAT_API_KEY=REPLACE_WITH_REAL_CHAT_API_KEY
HARBORRAG_EMBED_MODEL=text-embedding-3-small
HARBORRAG_EMBED_API_KEY=REPLACE_WITH_REAL_EMBED_API_KEY
```

Set `HARBORRAG_CHAT_MODEL` to a chat model your key can use and set both key lines.
You can use the same key for chat and embeddings. Keep the embedding model shown above
for OpenAI. For another OpenAI-compatible endpoint, use its model names and keys and set
`HARBORRAG_URL` to its base URL. Leave the other placeholders as they are. See the
[shipped model catalog](../users/configuration/model-config.md#the-shipped-catalog)
for the available options.

If your endpoint lists models at `/models`, you can check the chat key and the
available names before starting the stack.

```bash
(
  . env/.env.models
  curl -fsS -H "Authorization: Bearer $HARBORRAG_CHAT_API_KEY" \
    "${HARBORRAG_URL:-https://api.openai.com/v1}/models"
)
```

The response should list the model names you set. If you use a different embedding
key, run the check again with `HARBORRAG_EMBED_API_KEY` in the header.

### Point the CLI at PostgreSQL

Open `env/.env.database` and remove the `#` in front of `HARBORRAG_CONTROL_DB_URL`.

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

`up` starts the data services, the Temporal workflow engine, the ingestion workers
and the HarborRAG API. The first run takes several minutes to build the worker and API
images. Look for this line when the API is up.

```text
HarborRAG API is healthy at http://127.0.0.1:8000/healthz
```

On a new stack, the API imports the example *connections* from
`config/sources/connectors.yaml`. A connection tells HarborRAG what to read.
The example `harborrag-workspace` connection belongs to the `default` project and reads
`docs/` without source credentials.

<details markdown="1">
<summary>If a port is already in use</summary>

The default ports on `127.0.0.1` are 5432 (PostgreSQL), 6333 and 6334 (Qdrant),
6379 and 3000 (FalkorDB), 6380 (Redis), 9000 and 9001 (MinIO), 7233 and 8080
(Temporal and its UI), and 8000 and 9465 (the API and metrics).
Free the port or change its matching `*_PORT` value in `env/` and rerun `up`.
If you change the API port, use the new value in the next step.

</details>

## 4. Check everything is in place

Check that the API can reach its dependencies.

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
you have not set, so they may show the status `error` or `needs_reauth`; this tutorial
does not use them.

## 5. Ingest the documentation folder

*Ingestion* reads a connection's documents, splits them into passages, and stores each passage
as a vector in Qdrant and as nodes in the knowledge graph. Start it for
`default/harborrag-workspace` and wait for it to finish:

```bash
uv run harborrag ingestions start default/harborrag-workspace --wait
```

The command submits the run to the Temporal server on `localhost:7233`, and `--wait` keeps it
attached until the run ends. Each passage is sent to the embedding model.

While the run works, the command prints a line whenever the run's status or counts change:
the run's ID, its status, and how many documents were `discovered`, `processed`, `skipped`
and `failed`. The last line shows `succeeded`, and the command exits with status 0. A
document that fails is counted under `failed`; the rest of the run continues. If the whole
run fails or is cancelled, the command exits with status 8.

Run the same command again and the unchanged documents are counted as `skipped`: HarborRAG
only processes what changed.

If every document fails, check the model values from step 2. For other errors, see
[Troubleshooting](../users/troubleshooting/README.md).

## 6. Ask a question

Ask a question about the documents you just indexed.

```bash
uv run harborrag chat "Summarise what the HarborRAG documentation covers in three bullet points."
```

Look for citations such as `[1]` in the answer and the corresponding document
titles below it.

<details markdown="1">
<summary>If the answer has no citations</summary>

Run `uv run harborrag doctor` and check that `corpus mode` says `tenant_shared`.
The tutorial uses shared access so callers in `DEFAULT` can search all of its published
documents. For a local stack created from an older template, set these lines in
`env/.env.api` and run `scripts/deployment/dev.sh api` to apply them to the API. The CLI
reads the same file on its next run.

```dotenv
HARBORRAG_CORPUS_ACCESS_MODE=tenant_shared
HARBORRAG_CORPUS_SHARED_TENANT_ID=DEFAULT
```

For deployments where readers need individual source permissions, see
[Read access inside a tenant](../users/configuration/workspace-mode.md#read-access-inside-a-tenant).

</details>

When a document changes, run `ingestions start` again before asking about its new content.

## What you built

You ran HarborRAG locally, indexed the example documents and asked a question with
citations. Stop the stack with `scripts/deployment/dev.sh down`. Add `--volumes` only
when you want to discard the stored data.

### Where to go next

- [Running HarborRAG from a checkout](../developers/checkout-quick-start.md) - restart and
  rebuild single services, drive ingestion runs, and run the tests.
- [Connections](../users/connections.md) - add Jira, Confluence, SharePoint or GitHub sources.
- [MCP quick start](../users/detailed-guides/mcp-server/setup-and-integration.md) -
  search HarborRAG from an IDE or an agent.
- [CLI reference](../users/cli-reference/README.md) - every command and option.
- [Troubleshooting](../users/troubleshooting/README.md) - fixes for common failures.
