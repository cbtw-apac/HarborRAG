# Quick Start

This is the guide to read if you have never used HarborRAG before. In about ten minutes you
will install it, point it at a folder of documents, and ask questions over them. Nothing
here needs a copy of the source code.

## Before you start

You need three things:

| | What | How to check |
| --- | --- | --- |
| 1 | **Python 3.12 or newer** | `python3 --version` |
| 2 | **Docker with Compose v2** - runs the three small services HarborRAG stores data in | `docker compose version` |
| 3 | **An API key** for one model provider: OpenAI, Azure OpenAI, Google Gemini, or an OpenAI-compatible gateway | you will paste it once, into a local file |

The key is used for two things: turning your documents into embeddings during ingestion,
and answering with `harborrag chat`. A handful of documents costs cents.

## 1. Install

```bash
pip install "harborrag[local]"
harborrag --help
```

`[local]` brings the `harborrag` command and the client libraries for the local stack. The
help output lists five commands: `init`, `doctor`, `ingest`, `retrieve`, and `chat` - you
will use them in that order.

## 2. Create a project

A *project* is just a folder that holds HarborRAG's configuration. Create one:

```bash
harborrag init my-harbor
```

`init` asks a few questions. Press Enter to accept a default.

```text
Model provider [azure-openai/gemini/openai/openai-compatible] (openai):
Chat model (gpt-4o-mini):
Embedding model (text-embedding-3-small):
OPENAI_API_KEY (blank to fill in later):            ← paste your key; it is not echoed
Data sources to ingest (comma-separated: local, github, confluence, jira) (local):
Local folder to ingest (./docs):
```

Choose `local` for now - a folder on your disk is the fastest way to see results. You can add
GitHub, Confluence, or Jira later (see [Add more sources](#add-more-sources)).

When it finishes you get a numbered list of next steps. The folder now contains:

| File | What it is for | Will you edit it? |
| --- | --- | --- |
| `harborrag.yaml` | Marks the folder as a project; points at the files below | rarely |
| `.env` | Your API key, plus generated passwords for the local services. Never committed. | when a key changes |
| `config/connectors.yaml` | *Where* documents come from - one entry per source | when adding sources |
| `config/models.yaml` | *Which* chat and embedding models to use | when switching models |
| `config/parsers.yaml` | *How* PDFs, Office files and images are read | almost never |
| `config/temporal.yaml` | Only for the durable, worker-based mode - ignore for now | no |
| `docker-compose.yml` | The three local services | no |
| `docs/README.md` | A sample document so your first run indexes something | replace with your files |

> **Scripting it:** every question has a flag. `harborrag init my-harbor --yes --api-key
> sk-… --source ./notes --connectors local,github` creates the same project without prompts.

## 3. Start the local services

```bash
cd my-harbor
docker compose up -d
```

This starts Qdrant (vectors), FalkorDB (the knowledge graph), and MinIO (a copy of every
source file). Data lives in Docker volumes, so it survives `docker compose down`.

> **Ports already taken?** If something else on your machine already uses 6333, 6379 or
> 9000, `init` noticed and moved the whole stack to free ports (it tells you: `Using port
> offset 10000 …`). Nothing to do - `.env` and `docker-compose.yml` already agree.

## 4. Check everything is in place

```bash
harborrag doctor
```

Every line should be a ✓ except the last, which is skipped on purpose:

```text
╭───────────────────────────── ✓ Ready ──────────────────────────────╮
│  ✓  project              /home/you/my-harbor                        │
│  ✓  python packages      local stack clients importable            │
│  ✓  connectors catalog   1 enabled: workspace                       │
│  ✓  parsers catalog      …/config/parsers.yaml                      │
│  ✓  models catalog       chat=primary embed=primary                 │
│  ✓  source path          LOCAL_SOURCE_PATH=./docs                   │
│  ✓  qdrant               http://localhost:6333                      │
│  ✓  falkordb             localhost:6379                             │
│  ✓  object store         http://localhost:9000                      │
│  ✓  control plane        ready                                      │
│  –  temporal             client not installed; only needed for …    │
╰────────────────────────────────────────────────────────────────────╯
```

If a line shows ✗, the fix is printed under the table. The common ones:

| Line | Detail | What to do |
| --- | --- | --- |
| `models catalog` | `OPENAI_API_KEY is blank` | Open `.env`, paste the key after `OPENAI_API_KEY=`, run `doctor` again |
| `qdrant` / `falkordb` / `object store` | `… unreachable` | Run `docker compose up -d` in the project folder and wait ~15 seconds |
| `python packages` | `missing: qdrant-client, …` | `pip install "harborrag[local]"` |
| `source path` | `LOCAL_SOURCE_PATH does not point to an existing directory` | Fix the path in `.env` or create the folder |
| `connector credentials` | `blank: github: GITHUB_TOKEN` | Fill in the named variables in `.env` (only when you added a remote source) |
| `project` | `no harborrag.yaml found` | You are outside the project - `cd my-harbor` or add `--project my-harbor` |

## 5. Ingest your documents

Put a few Markdown, text, PDF, Word, PowerPoint, CSV or Excel files into `docs/` (or keep the
sample), then:

```bash
harborrag ingest run workspace
```

`workspace` is the name of the local-folder connector `init` created. You will see a live
block that updates in place:

```text
⚓ workspace · ingest-3f2a9c… · RUNNING · 00:12
✓ Discover  ↻ Preflight  ↻ Fetch  ↻ Parse  ↻ Chunk  ↻ Index  ↻ Validate  ↻ Finalize  ○ Reconcile
████████████████████░░░░░░░░░░░░░░░░░░░░  4/8 documents
succeeded 4 · unchanged 0 · skipped 0 · failed 0
```

followed by a summary panel that ends with `Ingestion completed`. Running the same command
again is fast and reports the files as `unchanged` - HarborRAG only re-processes what changed.

A document that fails is listed by name with the reason; the rest of the run continues.

## 6. Ask a question

Search returns the passages that match, with their scores and source:

```bash
harborrag retrieve "what does this project do" --top-k 3 --include-content
```

Chat answers in prose, grounded in those passages, using the chat model you chose:

```bash
harborrag chat "Summarise what these documents cover in three bullet points."
```

That is the whole loop: **edit files → `ingest run` → `retrieve` / `chat`.**

## Add more sources

Run `init` again in the project folder and pick additional sources; it asks whether to
overwrite the configuration (your `.env` is never overwritten - a fresh copy goes to
`.env.new` so you can merge the new variables). Or edit by hand:

1. Add a block to `config/connectors.yaml` - `init --connectors github` shows the shape, and
   [Connector configuration](../users/configuration/connector-config.md) lists every option.
2. Add the variables it references (URL, token, email) to `.env`.
3. `harborrag doctor` confirms nothing is blank, then `harborrag ingest run github`.

Each source is ingested with its own name, so `retrieve` and `chat` see all of them together.

## Everyday questions

**Do I have to run commands from inside the project?** No. HarborRAG looks upward from your
current folder for `harborrag.yaml`, so `cd my-harbor/docs && harborrag doctor` works. From
somewhere else, pass `--project /path/to/my-harbor`. For safety it only trusts a project
folder that *you* own and that other users cannot write to; `--project` is the explicit
override.

**Where is my API key stored?** Only in `.env` in the project folder (permissions `0600`).
`config/models.yaml` refers to it as `${OPENAI_API_KEY}` and never contains the value.

**How do I switch models or providers?** Edit `config/models.yaml` (model name, provider) and
the matching key in `.env`; `harborrag doctor` validates the result. Changing the *embedding*
model means re-ingesting with `--force-reprocess`.

**How do I start over?** `docker compose down -v` in the project folder deletes the indexed
data; `rm -rf .harborrag` deletes the local bookkeeping database. Your files and `.env` are
untouched.

**What about the "durable" mode I see in the docs?** `ingest start`, `status`, `pause`,
`resume`, `cancel` and `watch` run ingestion through Temporal workers for very large or
long-running sources. They need the full stack from the repository checkout - see
[Running HarborRAG from a checkout](../developers/checkout-quick-start.md#run-the-full-stack). For a folder,
a repository, or a Confluence space, `ingest run` is all you need.

## Where next

- [CLI reference](../users/cli-reference/README.md) - every command and flag
- [Configuration](../users/configuration/README.md) - connectors, models, parsers in depth
- [Python SDK](../users/python-sdk/README.md) - the same project, used from your own code
- [MCP tools](../users/detailed-guides/mcp-server/README.md) - expose retrieval to an IDE or agent
- [Troubleshooting](../users/troubleshooting/README.md)
- [Running HarborRAG from a checkout](../developers/checkout-quick-start.md) - for contributors, and for the durable Temporal mode
