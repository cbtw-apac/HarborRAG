# Troubleshooting

Find the symptom you see, its likely cause, and the fix. Commands are written as you run
them from inside a clone of the repository, with `uv run harborrag …`.

## Start with `harborrag doctor`

`doctor` checks the setup one line at a time: the project, the Python clients, the
connector, parser and model catalogs, the local source root, the credential variables of
remote connectors, and the local services, Temporal included. It ends with the tenants
that hold data.

```bash
uv run harborrag doctor
```

Each ✗ line names the problem, and the hint under it names the `env/.env.*` file or the
`scripts/deployment/dev.sh` command that fixes it. `doctor` exits 1 while a required
configuration check fails, and 6 when only service checks fail; a warning does not count.
Like the other commands, it walks up from the current directory to the clone, so you can
run it from any folder inside. The
[CLI reference](../cli-reference/README.md#check-the-setup) lists every check.

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| `source path` warns that `HARBORRAG_LOCAL_SOURCE_ROOT` does not name a directory on this host | The value is a container path such as `/data/sources`, which only the API and workers have | Expected when you ingest through the stack; `connections test` on a `local_file` connection reports `"ok": false` and exits 8 for the same reason. Set the variable to a folder on this machine to test locally |
| `source path` warns that `HARBORRAG_LOCAL_SOURCE_ROOT` is unset | The CLI accepts any `local_file` path, but the API refuses every `local_file` connection while the root is unset | Set `HARBORRAG_LOCAL_SOURCE_ROOT` in the API and worker environment, and for the CLI on its line in `env/.env.database` |
| `connector credentials` warns `blank credentials for: …` | An enabled remote connector (Jira, Confluence or SharePoint) has blank URL or credential variables | Nothing to fix if you do not use them. Otherwise fill them in `env/.env.connector`, or set `enabled: false` on the connectors you do not use in `config/sources/connectors.yaml` |

## Report a problem

Include the output of `harborrag --version`, `harborrag doctor` and the commit of your
clone in a bug report. The version names the release line and works even when the project
directory or the services are broken; a checkout of `main` prints the same version as the
release, so the commit tells us which code you run.

```bash
uv run harborrag --version
git rev-parse --short HEAD
```

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
| `python -m harborrag_adapters.models validate` reports a missing environment variable, or exits 2 with `configuration error: <VARIABLE> is blank` | `config/models/models.yaml` expands every `${VARIABLE}` when it loads, and `validate` refuses one that is set but blank, in the family it checks, as `harborrag doctor` does | Set every variable the selected family uses to a value, for example with `uv run --env-file env/.env.models` |
| `validate --family rerank` exits 2 with `String should have at least 1 character` | Reranking is off: the shipped rerank section names no model until `HARBORRAG_RERANKER_MODEL` is set | Nothing to fix while you do not rerank; set the variable before you check that family |
| `model configuration has unknown top-level key(s): …` | The model catalog holds a section other than `chat`, `embed` and `rerank`, such as a misspelled one or the removed `profiles:` | Fix the spelling or remove the section; see [What the catalog refuses](../configuration/model-config.md#what-the-catalog-refuses) |
| A log or an error says `deployment '…' has an empty api_key; set the variable it reads` | The key variable of that model deployment is set but blank or only whitespace. HarborRAG refuses it when it builds the model's client, instead of letting LiteLLM send another key | Fill the variable in `env/.env.models`, or in the deployment's secret manager, and restart the API, the worker and the MCP server |
| A model server answers `401` for a deployment that has no `api_key` in a catalog of your own | The deployment sends the placeholder `harborrag-no-api-key` instead of an environment key such as `OPENAI_API_KEY`, which LiteLLM sent in earlier releases | Give the deployment the key it needs, such as `api_key: ${OPENAI_API_KEY}`, and restart the API, the worker and the MCP server; see [What the catalog refuses](../configuration/model-config.md#what-the-catalog-refuses) |
| The MCP server stops at startup, or the API fails search and chat, with `HARBORRAG_RERANKER_API_KEY is empty, and the rerank model needs it`; `harborrag doctor` and `validate --family rerank` fail with the same message | `HARBORRAG_RERANKER_MODEL` is set and the reranker key is blank. The shipped rerank deployment reads its key from that variable, and a key read from a blank variable is refused for every provider, even for a gateway that takes rerank calls without one | Set `HARBORRAG_RERANKER_API_KEY` in `env/.env.models`, or unset `HARBORRAG_RERANKER_MODEL` to turn reranking off, then restart the API, the worker and the MCP server |
| `PATCH /v1/admin/settings` refuses `retrieval.rerank: true` with `422 validation_error` | The model catalog names no rerank model, as the shipped one does not until `HARBORRAG_RERANKER_MODEL` is set, or the API started before it was. A blank `HARBORRAG_RERANKER_API_KEY` has the same effect | Set `HARBORRAG_RERANKER_MODEL` and `HARBORRAG_RERANKER_API_KEY` in `env/.env.models` and restart the API; see [Rerank chat retrieval](../configuration/runtime-settings.md#rerank-chat-retrieval) |
| The API, the worker, the MCP server or the CLI stops at startup with `HARBORRAG_QDRANT_URL names a port other than 6333, but gRPC would still dial port 6334 on that host` | `HARBORRAG_QDRANT_URL` names another REST port, such as a mapped `16333`, while gRPC is preferred and `HARBORRAG_QDRANT_GRPC_PORT` is unset, so gRPC would reach port 6334, which can belong to another Qdrant | Wherever `HARBORRAG_QDRANT_URL` is set, set `HARBORRAG_QDRANT_GRPC_PORT` to the gRPC port of the same Qdrant, or `HARBORRAG_QDRANT_PREFER_GRPC=false`. For the Compose API and worker, that is `env/.env.temporal`; see [Ingestion runtime environment](../configuration/config-file-reference.md#ingestion-runtime-environment) |
| `Enabled parser definitions '…' and '…' both configure family 'pdf'` | More than one enabled definition replaces the same parser | Enable only one definition per parser family |
| Unknown fields are rejected | Configuration models use strict validation | Fix the spelling, or move Python-only callbacks into code overrides |

Example files (`*.example`, `*.example.yaml`) are not loaded automatically: nothing reads
them unless you name them. From a clone, the CLI reads the `env/` files listed in
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
| `harborrag: no project found. Run it inside a HarborRAG repository checkout, or pass --project DIR for a directory containing harborrag.yaml.` in a terminal or with `-o table`, or the error envelope with `internal_error` and that message elsewhere, with exit 1 | The command ran outside a clone of the repository | `cd` into the clone, or any folder inside it, and run the command again; see [Run from a repository checkout](../cli-reference/README.md#run-from-a-repository-checkout) |
| `Refusing to ingest or change connections from a repo checkout in the local sqlite control DB` | The CLI in a clone uses a local SQLite file until you point it at the stack's Postgres; `doctor` warns `using the local sqlite fallback` | Uncomment the `HARBORRAG_CONTROL_DB_URL` line in `env/.env.database`, or export the variable; see [Run from a repository checkout](../cli-reference/README.md#run-from-a-repository-checkout) |
| `Invalid value for REF: expected PROJECT_ID/CONNECTION_ID` | The command was given a bare key such as `harborrag-workspace`, or a source type such as `local` | Pass the connection's ref, such as `default/harborrag-workspace`; `uv run harborrag connections list` lists them |
| `ingestions start` fails with `422 reference_not_found` | The project or the connection does not exist in the tenant; an imported connection exists only in the tenant `HARBORRAG_TENANT_ID` named at the first start, `DEFAULT` unless set | Check the ref and `--tenant` with `uv run harborrag connections list`; see [Run and control ingestion](../cli-reference/README.md#run-and-control-ingestion) |
| `connections get` fails with `404 not_found` and exit 3 | No connection has that ref in the tenant | Check the ref and `--tenant` with `uv run harborrag connections list` |
| An old subcommand such as `harborrag source list` exits 2 with `was renamed` | The command was renamed in 2.0.0 | Run the command the message names; see [Upgrading](../upgrading.md#update-scripts-for-cli-v200) |
| `ingestions start` fails with `Tenant … has no data yet; pass --yes to create it` | The write goes to a tenant that holds no data, without a terminal to ask | Check `--tenant`, `HARBORRAG_TENANT_ID` and the CLI profile for a typo; pass `--yes` only to create the tenant |
| An ingestion command cannot connect to Temporal | `HARBORRAG_TEMPORAL_TARGET` does not name a reachable Temporal frontend | Set it, normally to `localhost:7233` from the host; `ingestions start --inline` needs no Temporal |
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
CLI, `uv run harborrag ingestions get INGESTION_ID` reads the same run.

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
`uv run harborrag ingestions cancel INGESTION_ID`, not with Terminate in the Temporal
UI. A terminated workflow never records its end, so the run stays `running`.

Before HarborRAG refuses a start, it asks Temporal about the running ingestion. A run
whose workflow ended is recorded as `cancelled` (terminated or cancelled) or `failed`
(failed, timed out, or completed without recording its end), and the start goes ahead.
Removing the connection or cancelling the run does the same; until one of these
happens, the run still reads `running`. A run that Temporal reports as running or
paused, or cannot describe, still blocks its connection. The `409` names the blocking
run in `details.ingestion_id`, also for `ingestions start --inline`. It is `null` when
another start of the same connection is still registering its run.

A run whose workflow Temporal no longer has, for example after its history passed the
namespace retention, stays `running` until you cancel it. Cancelling a running or paused
run records it as `cancelled` with `error_category` `workflow_not_found`.

## Retrieval, chat and MCP

Chat answers over HTTP and the CLI only; the MCP server exposes retrieval tools only.

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| HTTP chat or `uv run harborrag chat` answers that no sources were retrieved, or its answer has no citations although content is ingested | The tenant runs `source_acl`, where a caller with no source permissions gets no sources. The shipped `env-example/.env.api.example` sets `tenant_shared`, so an `env/.env.api` created from an older template, or a tenant other than the shared one, lands here. `uv run harborrag doctor` shows the `corpus mode` the CLI uses | Add `HARBORRAG_CORPUS_ACCESS_MODE=tenant_shared` and `HARBORRAG_CORPUS_SHARED_TENANT_ID=DEFAULT` to `env/.env.api` and restart the API; the CLI reads those two lines from the same file, and an exported variable outranks it. See [Ask from the CLI](../chat/README.md#ask-from-the-cli) and [Read access inside a tenant](../configuration/workspace-mode.md#read-access-inside-a-tenant) |
| Chat fails with `environment variable HARBORRAG_CHAT_API_KEY is not set`, or names another model variable | `config/models/models.yaml` expands the key and the model names before the first call | Fill in `env/.env.models`, or inject the same values through the deployment's secret manager; see [Model configuration](../configuration/model-config.md#the-shipped-catalog) |
| Answers got worse after a change of `HARBORRAG_EMBED_MODEL`, with no error | The stored vectors come from the previous embedding model; both models write 1536 dimensions, so nothing fails | Re-ingest every connection with `uv run harborrag ingestions start PROJECT_ID/CONNECTION_ID --mode force`; see [Model configuration](../configuration/model-config.md#the-shipped-catalog) |
| HTTP chat returns `409 no_indexed_content` | Nothing is published in the session's or the question's projects | Ingest a connection of that project first; see [Chat](../chat/README.md#handle-errors) |
| HTTP chat returns `503 llm_unavailable` | Model configuration, credentials, provider reachability or a provider limit failed; provider details are not returned to callers | Read the API logs, and check the chat family with `uv run --env-file env/.env.models python -m harborrag_adapters.models validate config/models/models.yaml --family chat` |
| The API answers `503 dependency_unavailable` | A store, such as Qdrant, FalkorDB or the S3 object store (MinIO), is down or timing out. `details.dependency` names it when known | Start or fix the store; `uv run harborrag doctor` checks each one. Ingestion retries such failures on its own |
| An MCP client gets JSON-RPC error `-32000` from `/mcp`, or `/api/tools` answers `429` with a `Retry-After` header | The credential sent more than `HARBORRAG_MCP_RATE_LIMIT` requests per second (10 by default), or had more than `HARBORRAG_MCP_MAX_INFLIGHT` requests running at once (4 by default). Both limits count `/mcp` and `/api/tools` together | Retry after the wait, or slow the client down. To raise a limit, set its variable in `env/.env.mcp` and restart the server; `0` turns that limit off. See [MCP runtime environment](../configuration/config-file-reference.md#mcp-runtime-environment) |
| `harborrag-mcp` exits at startup with `durable MCP audit directory must be owner-only (mode 0700)`, or `durable MCP audit directory must be owned by this process user` | The directory of the durable audit file, `~/.harborrag` unless `HARBORRAG_MCP_AUDIT_PATH` names another, grants access to other users or belongs to another user. An earlier `harborrag auth keys create` or SharePoint sign-in may have created it with a mode such as 0755 | Run `chmod 700` on the directory the message names, as its owner, or set `HARBORRAG_MCP_AUDIT_PATH` to a file in an owner-only directory. HarborRAG never changes the mode of an existing directory |
| MCP appears to do nothing when started in a terminal | The stdio MCP server waits for a client over pipes and has no port or prompt | Run `scripts/deployment/mcp.sh --check`, or start it with `--http` and open `http://127.0.0.1:8010/` |
| `mcp.sh --http` prints `HarborRAG MCP preflight failed` and starts nothing | A setting or backend the server needs at start-up is missing or unreachable. The line names it, and a `Note:` line above it lists model variables that no env file sets | Fix what the line names and run the command again; see [Checks before the HTTP server starts](../detailed-guides/mcp-server/setup-and-integration.md#checks-before-the-http-server-starts) |
| The MCP preflight line ends in `(cause: InvalidPasswordError)` or `(cause: InvalidAuthorizationSpecificationError)` | Postgres refused the server's read-only role, `harborrag_mcp`: its password is wrong or missing, or the role cannot log in yet, for example on a new database volume or after an upgrade | Run `scripts/deployment/dev.sh mcp-access`, then start the server again; see [Read-only database and object-store accounts](../detailed-guides/mcp-server/setup-and-integration.md#read-only-database-and-object-store-accounts) |
| Every `mcp.sh` or `mcp-ui.sh` command, `down` included, stops with `required variable POSTGRES_MCP_PASSWORD is missing a value: run scripts/deployment/dev.sh mcp-access to set POSTGRES_MCP_PASSWORD`, or the same for `MINIO_MCP_PASSWORD` | `env/.env.database` has no password for the MCP server's read-only accounts, as in a file older than these accounts | Run `scripts/deployment/dev.sh mcp-access` once the stack is up, or `scripts/deployment/dev.sh bootstrap` to only write the passwords |
| The MCP HTTP server answers `421 Misdirected Request` or `403 Forbidden Origin` | The request is addressed to a host name other than `127.0.0.1`, `localhost` or `::1`, for example through a reverse proxy, or it comes from a page on another origin | Call the server by a loopback name, or list the proxy's host name in `HARBORRAG_MCP_ALLOWED_HOSTS`, with `HARBORRAG_MCP_AUTH_MODE=api_key`, and restart it |
| The status page disables **Save**, or `PUT /api/config` answers `409` | The container mounts `config/` read-only | Edit `config/mcp/server.yaml` in the checkout and select **Reload YAML** |
| A tool run on the status page answers `tool execution failed (<ErrorType>)` | The tool failed inside the server. The page receives only the error type, as an MCP client does | The server log names the tool, the error type and where it was raised, without the error's message: `scripts/deployment/mcp.sh logs` |
| The browser cannot load or run tools | The owner token is missing or wrong, or the HTTP server is not running | Load `HARBORRAG_MCP_BEARER_TOKEN` from the git-ignored `env/.env.mcp`; never paste model API keys into the UI |
| MCP accepts the bearer token but retrieval is denied or empty | The token authenticates the client; the tenant runs `source_acl` (an `env/.env.mcp` created from an older template has no corpus lines), or the tool call names a tenant other than the shared one | For a deliberately shared corpus, set `HARBORRAG_CORPUS_ACCESS_MODE=tenant_shared` and `HARBORRAG_CORPUS_SHARED_TENANT_ID` to that tenant in `env/.env.mcp`, then restart MCP. 2.0.0 cannot import source and document permissions yet, so a tenant on `source_acl` reads nothing; see [Read access inside a tenant](../configuration/workspace-mode.md#read-access-inside-a-tenant) |
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
whole, so start from `uv run harborrag connections get PROJECT_ID/CONNECTION_ID -o yaml`;
see
[Manage projects and connections](../cli-reference/README.md#manage-projects-and-connections).

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
