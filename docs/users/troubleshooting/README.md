# Troubleshooting

Find the symptom you see, its likely cause, and the fix. Commands are written as you run
them from inside a clone of the repository, with `uv run harborrag …`.

## Start with `harborrag doctor`

`doctor` checks the setup one line at a time: the project, the Python clients, the
connector, parser and model catalogs, blank credential variables, the local source folder,
and the local services. Add `--temporal` to check Temporal too.

```bash
uv run harborrag doctor
```

Each ✗ line names the problem, and the hint under it names the `env/.env.*` file or the
`scripts/deployment/dev.sh` command that fixes it. `doctor` exits 1 while a required check
fails; a warning does not. In a clone, the `project` line warns
`no harborrag.yaml found; using the current directory`, which is expected. The
[CLI reference](../cli-reference/README.md#check-the-setup) lists every check.

## Installation and imports

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| `ModuleNotFoundError: harborrag_*` | The command ran outside the clone's environment, or the environment is out of date | Run `uv sync --all-packages --extra dev` in the clone, then run commands through `uv run` or activate `.venv` |
| `uv` cannot create a cache or lock file | The global uv cache is not writable | Point `UV_CACHE_DIR` at a writable directory; see the [uv cache documentation](https://docs.astral.sh/uv/concepts/cache/) |
| An optional parser or provider import fails | The package extra or the native runtime that backend needs is missing | Install the matching `harborrag-adapters[...]` extra and the backend's prerequisites; see [Installation](../../getting-started/installation.md#add-parser-and-pdf-extras) |
| PDF parsing downloads models or runs slowly | A heavier PDF profile is enabled | Keep the shipped `pdf-liteparse` definition or the commented `pdf-fast` profile in `config/ingestion/parsers.yaml`, and pre-download assets only for the backend you choose; see [Parser configuration](../configuration/parser-config.md) |

## Configuration

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| The connector catalog example fails to load | The path was copied without the `.example` suffix | Load `config/sources/connectors.example.yaml`; see [Load and build](../configuration/connector-config.md#load-and-build) |
| `Connector 'harborrag-workspace' requires environment variable 'LOCAL_SOURCE_PATH' for 'source_path'` | The example maps `source_path` to that variable | Export it, or pass `environment=` or `overrides=` to `catalog.build` |
| `python -m harborrag_adapters.models validate` reports a missing environment variable | `config/models/models.yaml` expands every `${VARIABLE}` when it loads | Set every variable the selected family and profile use, for example with `uv run --env-file env/.env.models` |
| `PATCH /v1/admin/settings` refuses `retrieval.rerank: true` with `422 validation_error` | The model catalog has no `rerank` section, or the API started before it was added | Add the `rerank` section, set `HARBORRAG_RERANKER_MODEL`, and restart the API; see [Rerank chat retrieval](../configuration/runtime-settings.md#rerank-chat-retrieval) |
| `Enabled parser definitions '…' and '…' both configure family 'pdf'` | More than one enabled definition replaces the same parser | Enable only one definition per parser family |
| Unknown fields are rejected | Configuration models use strict validation | Fix the spelling, or move Python-only callbacks into code overrides |

Example files (`*.example`, `*.example.yaml`) are not loaded automatically: nothing reads
them unless you name them. From a clone, the CLI reads
the `env/` files listed in
[Run from a repository checkout](../cli-reference/README.md#run-from-a-repository-checkout),
and the services get theirs from Docker Compose. Anything else reads only the process
environment: your shell, a container runtime or a secret manager.

## Ingestion and Temporal

An ingestion is one run that reads a connection's source and indexes its documents. A
connection is one configured data source, stored in the control database (the HarborRAG
Postgres database).

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| `harborrag` is not found | The console script is not on your `PATH` | Run `uv run harborrag` from the clone |
| `harborrag: no project found. Run it inside a HarborRAG repository checkout, or pass --project DIR for a directory containing harborrag.yaml.` | The command ran outside a clone of the repository | `cd` into the clone, or any folder inside it, and run the command again; see [Run from a repository checkout](../cli-reference/README.md#run-from-a-repository-checkout) |
| `Refusing to ingest or change connections from a repo checkout in the local sqlite control DB` | The CLI in a clone uses a local SQLite file until you point it at the stack's Postgres; `doctor` warns `using the local sqlite fallback` | Uncomment the `HARBORRAG_CONTROL_DB_URL` line in `env/.env.database`, or export the variable; see [Run from a repository checkout](../cli-reference/README.md#run-from-a-repository-checkout) |
| `ConnectionNotFoundError` from `ingest run` or `ingest start` | The argument is not the connection's `source_key`: a source type such as `local` is not one, a connection made through the API or `source add` uses `PROJECT/CONNECTION`, and an imported connection exists only in tenant `DEFAULT` | Use the key, such as `harborrag-workspace` or `default/harborrag-workspace`, without `--tenant`, or run `uv run harborrag source sync PROJECT/CONNECTION`; see [Name the connector](../cli-reference/README.md#name-the-connector) |
| An ingestion command cannot connect to Temporal | `HARBORRAG_TEMPORAL_TARGET` does not name a reachable Temporal frontend | Set it, normally to `localhost:7233` from the host; `ingest run` needs no Temporal |
| Temporal starts but no HarborRAG worker appears | The worker was not started | Run `scripts/deployment/dev.sh worker` |
| The Temporal worker exits at startup | A configuration path, a model credential or a store endpoint is wrong | Check the connector, parser and model configuration paths, the model credentials, and the Qdrant and FalkorDB endpoints |
| `HARBORRAG_SECRETS_ENCRYPTION_KEY or HARBORRAG_SECRETS_ENCRYPTION_KEY_FILE must be set when HARBORRAG_CONTROL_DB_URL is not SQLite` | The process has no key to encrypt and read stored connection secrets | Give the API and every worker the same key; `scripts/deployment/dev.sh bootstrap` generates one, or generate it by hand with `openssl rand -hex 32` |
| `dev.sh` stops with `HARBORRAG_SECRETS_ENCRYPTION_KEY is blank in env/.env.database, but … already holds a database` | A database already exists and may hold secrets written with an earlier key, so `dev.sh` will not generate a new one | Restore the key the database was written with |
| `secret ref cannot be decrypted` | The key changed after the secrets were stored | Restore the earlier key, or enter the connection's secrets again; see [Secrets](../connections.md#secrets) |

### An accepted ingestion appears to do nothing

A `202 Accepted` answer means the ingestion workflow was submitted; processing continues
in the background. Read the ingestion first:

```bash
curl --fail-with-body \
  http://127.0.0.1:8000/v1/admin/ingestions/INGESTION_ID
```

The local stack answers without a token. On a deployment that requires one, send the
`Authorization` header shown in [Connections](../connections.md#add-a-connection). From the
CLI, `uv run harborrag ingest status INGESTION_ID` reads the same run.

`counters.discovered` above 0 with `counters.processed` at 0 can mean the first bounded
batch of documents is still running; it does not mean the worker is idle. Follow the
worker logs and look for the same ingestion ID, logged as `task_id`, with its
`workflow_id`, stage and safe error code:

```bash
docker logs --follow harborrag-temporal-temporal-worker-1
```

See [`docker logs`](https://docs.docker.com/reference/cli/docker/container/logs/) for its
options. At `HARBORRAG_LOG_LEVEL=INFO`, HarborRAG logs API submissions, source discovery,
document outcomes, safe activity failures and finalization. `DEBUG` also logs each
activity's start, completion and duration. Every record names the logger, Python module,
function and source line. Logs never contain connector credentials, document content,
prompts or model output.

If the worker reports that earlier chunk or representation artifacts are missing during
a metadata-only update, current releases fall back to fresh encoding; this reuse is an
optimization and must not fail the ingestion. For a run an older worker created, let it
finish, deploy the current worker, and retry its retryable document failures with
`POST /v1/admin/ingestions/{ingestion_id}/retry-failures`.

### A stopped ingestion still answers `409 already_running`

Stop an ingestion with `POST /v1/admin/ingestions/{ingestion_id}/cancel` or
`uv run harborrag ingest cancel INGESTION_ID`, not with Terminate in the Temporal UI. A
terminated workflow never records its end, so the run stays `running`.

Before HarborRAG refuses a start, it asks Temporal about the running ingestion. A run whose
workflow ended is recorded as `cancelled` (terminated or cancelled) or `failed` (failed,
timed out, or completed without recording its end), and the start goes ahead. Removing
the connection or cancelling the run does the same; until one of these happens, the run
still reads `running`. A run that Temporal reports as running or paused, or cannot
describe, still blocks its connection.

A run whose workflow Temporal no longer has, for example after its history passed the
namespace retention, stays `running` until you cancel it. Cancelling a running or paused
run records it as `cancelled` with `error_category` `workflow_not_found`.

## Retrieval, chat and MCP

Chat answers over HTTP and the CLI only; the MCP server exposes retrieval tools only.

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| `uv run harborrag retrieve` or `chat` returns nothing from a clone | The CLI does not read `env/.env.temporal`, so it keeps the default `source_acl` access mode, in which its principal may read no source | Export `HARBORRAG_CORPUS_ACCESS_MODE=tenant_shared` and `HARBORRAG_CORPUS_SHARED_TENANT_ID=DEFAULT`, the values the worker template sets; see [Retrieve evidence](../cli-reference/README.md#retrieve-evidence) |
| Chat fails with `environment variable HARBORRAG_URL is not set`, or names another gateway value | `config/models/models.yaml` expands the gateway URL, key and model names before the first call | Fill in `env/.env.models`, or inject the same values through the deployment's secret manager; see [Model configuration](../configuration/model-config.md#the-shipped-gateway-catalog) |
| Answers got worse after a change of `HARBORRAG_EMBED_MODEL`, with no error | The stored vectors come from the previous embedding model; both models write 1536 dimensions, so nothing fails | Re-ingest every source with `--force-reprocess`; see [Model configuration](../configuration/model-config.md#the-shipped-gateway-catalog) |
| HTTP chat returns `409 no_indexed_content` | Nothing is published in the session's or the question's projects | Ingest a connection of that project first; see [Chat](../chat/README.md#handle-errors) |
| HTTP chat returns `503 llm_unavailable` | Model configuration, credentials, provider reachability or a provider limit failed; provider details are not returned to callers | Read the API logs, and check the chat family with `uv run --env-file env/.env.models python -m harborrag_adapters.models validate config/models/models.yaml --family chat` |
| MCP appears to do nothing when started in a terminal | The stdio MCP server waits for a client over pipes and has no port or prompt | Run `scripts/deployment/mcp.sh --check`, or start it with `--http` and open `http://127.0.0.1:8010/` |
| The browser cannot load or run tools | The owner token is missing or wrong, or the HTTP server is not running | Load `HARBORRAG_MCP_BEARER_TOKEN` from the git-ignored `env/.env.mcp`; never paste model API keys into the UI |
| MCP accepts the bearer token but retrieval is denied or empty | The token authenticates the client; `source_acl` remains the default reader policy, or the tool call names another tenant | For a deliberately shared corpus, set `HARBORRAG_MCP_READER_TENANT_ID`, `HARBORRAG_CORPUS_ACCESS_MODE=tenant_shared` and `HARBORRAG_CORPUS_SHARED_TENANT_ID` to the same tenant, then restart MCP. For restricted corpora, import current source and document ACLs instead |
| A tool change is saved but clients still list the old schema | FastMCP snapshots the advertised schemas at startup | Restart when the UI reports `restart_required=true` |

The MCP configuration UI changes enablement, defaults, numeric limits and tenant overrides
in `config/mcp/server.yaml`. It cannot change provider credentials, provider endpoints or
stored prompt files. See
[MCP Setup and Integration](../detailed-guides/mcp-server/setup-and-integration.md).

## Unexpected logs or oversized embedding input

An error such as `Too many input tokens. Max input tokens: 8192` means the text sent to
the embedding provider exceeded that model's input limit. A character or file-size limit
does not guarantee a safe token count, especially for timestamps, stack traces, escaped
JSON, ANSI sequences and other dense log syntax.

HarborRAG indexes whatever the connection admits; it does not drop timestamped or
error-like text as system output. The default `allowed_extensions` already leaves out
`log` and `jsonl` files, so logs arrive only when a connection lists them, sends
`"allowed_extensions": []`, or stores them under an admitted type such as `txt`.

If logs are not knowledge sources, exclude them in the connection's `config` with
`excluded_extensions`, `exclude_paths` and `exclude_globs` (for example
`["**/*.log", "**/logs/**", "**/*.jsonl"]`), then re-ingest. An update replaces `config`
whole, so start from `uv run harborrag source show PROJECT/CONNECTION --json`; see
[Manage connections](../cli-reference/README.md#manage-connections).

If logs are meant to be searched, keep the chunk limits below the embedding model's input
limit, with room for provider-added formatting: `limits.maximum_tokens` in
`config/ingestion/chunking.yaml`, or the `chunking.max_tokens` runtime setting that caps
it. See [Chunking configuration](../configuration/chunking-config.md). Re-ingest after
changing filters or chunk limits; a chunk that already failed is not repaired in place.

## Smoke checks against real services

Smoke checks are manual scripts outside normal pytest discovery that need the matching
extra, credentials and a reachable service. Exit code 2 means a prerequisite is missing,
not a failed check. See
[Real-system smoke checks](../../developers/testing/README.md#real-system-smoke-checks).

## Quality gates

| Failure | What to check |
| --- | --- |
| `make coverage` below 90% | Add package-local tests for the new source branches |
| `make deps-check` | Compare the import against the architecture's allowed-import table |
| `make typecheck` | Source functions need complete annotations under the root mypy policy |
| `make lint` | It runs `ruff format --check` and `ruff check`; run `make format` only when you mean to rewrite files |
| Website build | Check relative Markdown links and run the root website tests |

## Deployment surprises

The database and Temporal Compose stacks are for local development, not a production
topology: use Temporal Cloud or the official Helm chart with managed persistence. See
[Deployment](../../developers/deployment/README.md) before you build an application image.

The API stops at startup with `control-plane database is at schema revision X, newer than
this build's head Y` when a newer build (another branch or image) already migrated the
same control database. Run that newer build, or back up the database and downgrade it to
`Y` with the newer build's migrations; this build cannot, because it does not ship the
newer revisions. To keep builds from migrating each other's schema, give each its own
`POSTGRES_DB`.
