# Upgrading

This page lists the steps each release needs when you upgrade an existing deployment,
newest release first. The [changelog](../../CHANGELOG.md) lists everything that changed;
entries marked **Breaking** there are the ones these steps deal with. A first install
needs none of these steps: start with the
[Quick Start](../getting-started/quick-start.md).

## 2.0.0a2 to 2.0.0

Connections move from `config/sources/connectors.yaml` into the control database and are
managed through the API ([Connections](connections.md)). Ingestion reads each connection
at the start of every run.

Work through the sections in order: prepare, change the configuration, check the
settings 2.0.0 refuses, upgrade the services, then finish the steps after the upgrade.
The sections from [Update scripts for CLI v2.0.0](#update-scripts-for-cli-v200) on list
what behaves differently afterwards and what scripts and clients must change.

### Before you start

- Plan a maintenance window. The API, the MCP servers and the workers stay stopped while
  the control database migrates; see
  [Upgrade the services in order](#upgrade-the-services-in-order).
- Workers now read connections, revisions and stored secrets from the control database.
  Give every worker the same `HARBORRAG_CONTROL_DB_URL` and
  `HARBORRAG_SECRETS_ENCRYPTION_KEY` as the API, or point both at the same file with
  `HARBORRAG_SECRETS_ENCRYPTION_KEY_FILE`. A worker refuses to start without a key
  against a non-SQLite database. `scripts/deployment/dev.sh` generates a key only while
  no database volume exists. With a blank key and an existing volume, which is the case
  on an upgrade, it stops with
  `… already holds a database; restore the key it was written with.` Keep a backup of
  the key. A key with spaces at its start or end is still used as written, so stored
  secrets stay readable, and a warning now says so.
- Check that Postgres accepts the extra connections. `HARBORRAG_CONTROL_DB_POOL_SIZE`
  (default 40) and `HARBORRAG_CONTROL_DB_MAX_OVERFLOW` (default 32) apply to each
  control-database pool, and one process opens several. The API's control-plane engine
  now uses them too, where it used SQLAlchemy's default of 5 plus 10, so each API replica
  can open up to 57 more connections. Workers, and inline runs from the CLI, open a new
  pool of the same size to read connections and their secrets, so each worker process
  can open up to 72 more. Size `max_connections` for every pool of every API replica,
  worker and checkout CLI, or lower the two variables; see
  [Ingestion runtime environment](configuration/config-file-reference.md#ingestion-runtime-environment).
- Give the API the environment variables `config/sources/connectors.yaml` names, for
  example `JIRA_BASE_URL` or `LOCAL_SOURCE_PATH`. The one-time import reads non-secret
  values from the API's environment.

### Move the configuration files

`config/` now has one folder per domain. Move each file your deployment uses:

| Old path | New path |
| --- | --- |
| `config/connectors.yaml` | `config/sources/connectors.yaml` |
| `config/connectors.example.yaml` | `config/sources/connectors.example.yaml` |
| `config/schedules.yaml` | `config/sources/schedules.yaml` |
| `config/schedules.example.yaml` | `config/sources/schedules.example.yaml` |
| `config/parsers.yaml` | `config/ingestion/parsers.yaml` |
| `config/chunking.yaml` | `config/ingestion/chunking.yaml` |
| `config/topology/graph_build.yaml` | `config/graph/build.yaml` |
| `config/topology/ontologies/` | `config/graph/ontologies/` |
| `config/models.yaml` | `config/models/models.yaml` |
| `config/models.advance.example.yaml` | `config/models/models.production.example.yaml` |
| `config/mcp.yaml` | `config/mcp/server.yaml` |
| `config/mcp_keys.yaml` | `config/mcp/keys.yaml` |
| `config/mcp_keys.example.yaml` | `config/mcp/keys.example.yaml` |

`config/settings.yaml`, `config/settings.example.yaml` and `config/temporal.yaml` stay
where they are. `config/temporal.example.yaml` and `config/models.example.yaml` are
removed; [Temporal configuration](../../deploy/temporal/README.md) and
[Model configuration](configuration/model-config.md) now explain the values.
`config/harborrag.example.yaml` is removed too, with no replacement.

- A deployment that sets the path of every file it uses keeps working without moving
  anything: `HARBORRAG_CONNECTOR_CONFIG_PATH`, `HARBORRAG_SCHEDULE_CONFIG_PATH`,
  `HARBORRAG_PARSER_CONFIG_PATH`, `HARBORRAG_CHUNKING_CONFIG_PATH`,
  `HARBORRAG_GRAPH_BUILD_CONFIG_PATH`, `HARBORRAG_MODEL_CONFIG_PATH`,
  `HARBORRAG_MCP_CONFIG_PATH` and `HARBORRAG_MCP_KEYS_PATH`. The images now set the new
  paths, so a deployment that relied on their old values must move the files or set the
  variables.
- An environment file that names an old path, such as
  `HARBORRAG_MCP_CONFIG_PATH=config/mcp.yaml`, now points at a missing file. Update it.
- Some files are optional. While its path variable is unset, a missing `chunking.yaml`
  or `graph/build.yaml` means the built-in defaults, and the graph-build defaults make
  no LLM calls; a missing `mcp/server.yaml` means the packaged MCP defaults. A missing
  `schedules.yaml` always means no declared schedules. A file left at its old path is
  therefore ignored without a warning. Run `harborrag doctor` after the move; it checks
  the connector, parser, model and chunking files. `harborrag topology config validate`
  is removed, so check `config/graph/build.yaml` as
  [Graph build configuration](configuration/graph-build-config.md#check-the-file)
  shows.
- A relative `source_path` written directly in `connectors.yaml` resolves against the
  file's folder, now `config/sources/`. Add a `../` or use an absolute path.
  Environment-backed paths such as `LOCAL_SOURCE_PATH` are unaffected.
- `config/sources/schedules.yaml` and `config/mcp/keys.yaml` are git-ignored, like
  `config/settings.yaml`. Keep them in your deployment's own version control.
- The development worker now mounts the whole `config/` read-only, like the API and the
  MCP server. A configuration change needs a worker restart, not an image rebuild.

### Rename the tenant variable

**Breaking:** `HARBORRAG_INGESTION_TENANT_ID` is renamed `HARBORRAG_TENANT_ID`, and it
now sets the default tenant of the API as well as the workers and the CLI. There is no
alias: a process that still has the old name set stops at start-up with
`HARBORRAG_INGESTION_TENANT_ID was renamed to HARBORRAG_TENANT_ID`. Rename it wherever
it is set: in `env/.env.api` and `env/.env.temporal` (the workers' file), which the
earlier templates set to `DEFAULT`, in a shell that exports it, and in your deployment
manifests. The compose files pass `HARBORRAG_TENANT_ID` (`DEFAULT` unless set) to the API
and the workers. An API credential that holds every tenant (`*`) and sends no
`X-Tenant-Id` header now acts in that tenant instead of always `DEFAULT`.

### Set the corpus mode for chat

The shipped `env-example/.env.api.example` now sets
`HARBORRAG_CORPUS_ACCESS_MODE=tenant_shared` and
`HARBORRAG_CORPUS_SHARED_TENANT_ID=DEFAULT`, so chat reads the whole `DEFAULT` tenant on
a new deployment. The code default stays `source_acl`, and nothing changes for an
existing deployment until you add the two lines to its `env/.env.api` and restart the
API. Without them, chat over HTTP and `harborrag chat` answer that no sources were
retrieved, because a caller with no source permissions reads nothing in `source_acl`.
The `harborrag` command reads these two lines from `env/.env.api` (or the file
`API_ENV_FILE` names); an exported variable outranks the file. Keep
`HARBORRAG_CORPUS_SHARED_TENANT_ID` equal to `HARBORRAG_TENANT_ID`, as the workers'
`env/.env.temporal` does. To keep `source_acl`, leave the lines out.
`uv run harborrag doctor` shows the mode in its `corpus mode` row. See
[Read access inside a tenant](configuration/workspace-mode.md#read-access-inside-a-tenant).

The shipped `env-example/.env.mcp.example` sets the same two lines, so a reader key for
`DEFAULT` reads the shared corpus like chat does. An existing `env/.env.mcp` keeps
`source_acl` until you add them and restart the MCP server.

### Set the local source root

**Breaking:** `LOCAL_SOURCE_PATH` no longer sets the folder that `local_file`
connections must stay inside. Only `HARBORRAG_LOCAL_SOURCE_ROOT` does. A deployment that
relied on the old fallback now has no root, so set it:

1. On the API and the workers, set `HARBORRAG_LOCAL_SOURCE_ROOT` to the folder as they
   see it. The compose files set it to `/data/sources`.
2. For the `harborrag` CLI run from a checkout, add the same line to
   `env/.env.database`:

    ```dotenv
    HARBORRAG_LOCAL_SOURCE_ROOT=/data/sources
    ```

`LOCAL_SOURCE_PATH` stays in use for the one-time `connectors.yaml` import. See
[Local files](connections.md#local-files).

### Set the model endpoint variables

**Breaking:** `config/models/models.yaml` now calls an OpenAI-compatible endpoint for chat
and one for embedding, OpenAI's by default. It keeps a model name and an API key per
family and no longer reads a provider. Update `env/.env.models`, or the secret manager
that injects it:

1. Remove the chat provider variable, `HARBOR_CHAT_PROVIDER`, or its `HARBORRAG_` form
   if you already renamed it. The shipped catalog no longer reads it: chat is called as
   an OpenAI-compatible endpoint. `HARBORRAG_EMBED_PROVIDER` is still read, and takes
   `openai`, `openai_compatible` or `cohere`; any other value stops the start-up.

2. Keep the model name and the key of each family, under the names step 3 gives them:

    | Variable | Value |
    | --- | --- |
    | `HARBORRAG_CHAT_MODEL` | The chat endpoint's name for the model |
    | `HARBORRAG_CHAT_API_KEY` | The chat endpoint's API key |
    | `HARBORRAG_EMBED_MODEL` | The embedding endpoint's name for the model |
    | `HARBORRAG_EMBED_API_KEY` | The embedding endpoint's API key; it may equal the chat key |
    | `HARBORRAG_URL` | Optional. The base URL of another OpenAI-compatible endpoint for every family, such as a LiteLLM proxy gateway. Unset, the endpoint is `https://api.openai.com/v1` |
    | `HARBORRAG_EMBED_PROVIDER` | Optional. `cohere` when the embedding model is a Cohere model |
    | `HARBORRAG_RERANKER_MODEL`, `HARBORRAG_RERANKER_API_KEY` | Optional. A rerank model at the same endpoint and its key; once the model is set, the key is required |

    Use the endpoint's own model names. Remove a provider prefix such as `openai/` from
    `HARBORRAG_EMBED_MODEL`: the embedding name is sent as written.

3. Rename every remaining variable with these prefixes, in `env/.env.models` and any
   other environment file, including those the memory model and the model smoke tests
   read, such as `HARBOR_CHAT_BACKEND`:

    | Old prefix | New prefix |
    | --- | --- |
    | `HARBOR_CHAT_` | `HARBORRAG_CHAT_` |
    | `HARBOR_EMBED_` | `HARBORRAG_EMBED_` |
    | `HARBOR_MEMORY_` | `HARBORRAG_MEMORY_` |

    ```bash
    sed -i -E 's/(^|[^A-Z_])HARBOR_(CHAT|EMBED|MEMORY)_/\1HARBORRAG_\2_/g' env/.env.models
    ```

    `HARBOR_SMOKE_*` and `HARBOR_GATEWAY_TENANT` keep their names.

The first four variables are required: while one is missing, loading the catalog
fails, for example with `environment variable HARBORRAG_EMBED_API_KEY is not set`. A key
variable that is set but blank is refused too; see
[Check the settings that are now refused](#check-the-settings-that-are-now-refused).
`HARBORRAG_EMBEDDING_MODEL` keeps its meaning: a logical model of the catalog, such as
`primary`, not an endpoint model name.

A deployment that sent everything through a LiteLLM proxy gateway keeps
`HARBORRAG_URL` and its model names. It renames `HARBORRAG_API_KEY` to the three key
variables, and adds `HARBORRAG_EMBED_PROVIDER=cohere` for a Cohere embedding model.
To rerank, set `HARBORRAG_RERANKER_API_KEY` beside `HARBORRAG_RERANKER_MODEL`, even for
a gateway that takes rerank calls without a key. With the model set and the key blank,
the MCP server stops at startup and the API fails search and chat, with
`HARBORRAG_RERANKER_API_KEY is empty, and the rerank model needs it`. See
[Send Cohere embeddings and reranking through a gateway](configuration/model-config.md#send-cohere-embeddings-and-reranking-through-a-gateway).
To call a provider through its own API, keep a catalog of your own, point
`HARBORRAG_MODEL_CONFIG_PATH` at it, and keep the variables it reads, renamed as in
step 3; see [Model configuration](configuration/model-config.md).
`env-example/.env.models.example` keeps commented examples for that case.

If you added a `memory` model to the shipped catalog, point its deployment at the
endpoint as `primary` is: `provider: openai_compatible`,
`api_base: ${HARBORRAG_URL:-https://api.openai.com/v1}`,
`api_key: ${HARBORRAG_CHAT_API_KEY}`, and the endpoint's name for the model in
`HARBORRAG_MEMORY_MODEL`. See
[Optional memory model](configuration/model-config.md#optional-memory-model).

#### Re-index when the embedding model changes

The shipped catalog expects an embedding model that writes 1536-dimension vectors. It
now names the embedding space after `HARBORRAG_EMBED_MODEL`, which changes the revision
the contextual index is keyed by, so plan one re-ingest with this upgrade. If the model
`HARBORRAG_EMBED_MODEL` names is not the one that wrote your stored vectors, nothing
reports an error, because both write the same size, but retrieval quality drops without
warning until every document is embedded again. Re-ingest every connection in `force`
mode, so unchanged documents are embedded again too. `harborrag connections list` lists
the connections; start each one by its ref, for example:

```bash
harborrag ingestions start default/harborrag-workspace --mode force --wait
```

Over HTTP, send `"mode": "force"` to `POST /v1/admin/ingestions`.

Until a source finishes, its documents are still searched with the old vectors. The same
holds whenever you change `HARBORRAG_EMBED_MODEL` later.

### Check the settings that are now refused

2.0.0 refuses some settings that 2.0.0a2 accepted or ignored. Check each one before you
start the new release.

- **Insecure remote connections are for development only.**
  `HARBORRAG_QDRANT_ALLOW_INSECURE_REMOTE`, `HARBORRAG_FALKORDB_ALLOW_INSECURE_REMOTE`,
  `HARBORRAG_TEMPORAL_ALLOW_INSECURE_REMOTE` and `connection.allow_insecure_remote` in
  `config/temporal.yaml` now apply only while `HARBORRAG_ENV=dev`, as the Redis and
  object-store switches already did. In `staging` and `prod`, a Qdrant that is not on
  loopback needs HTTPS, FalkorDB needs SSL and Temporal needs TLS, or HarborRAG refuses
  to connect to it. Turn on TLS for each remote service before you upgrade.
- **A blank metrics token refuses every scrape.** A `HARBORRAG_METRICS_SCRAPE_TOKEN` that
  is empty or only spaces now counts as unset, so `/metrics` on the ops port answers
  `401` to every request; before, a bare `Bearer` header read it. Set a non-empty token in
  `env/.env.api` and give it to Prometheus as its bearer token.
  `env-example/.env.api.example` ships the line blank, and `scripts/deployment/dev.sh`
  fills it in.
- **A blank model API key is refused.** A chat, embedding or rerank deployment whose
  `api_key` is empty or only spaces is refused when HarborRAG builds its client, with an
  error that names the deployment. Before, LiteLLM sent an ambient key such as
  `OPENAI_API_KEY` or `COHERE_API_KEY` to the configured endpoint. Every provider
  refuses it, the `litellm_proxy`, `vllm` and `infinity` rerank providers included.
  Spaces around `api_key`, `model`, `api_base`, `deployment_name` and `embedding_space`,
  such as the CR at the end of a line in a CRLF env file, are removed, and a line break
  inside one is refused.
- **The chat proxy key must be set.** A catalog of your own whose chat `backend.type` is
  `litellm_proxy` needs a non-empty `backend.proxy.api_key`; an empty one is refused when
  the catalog loads. The shipped catalog calls chat with `direct_sdk` and is not
  affected.
- **The model catalog is checked strictly.** `config/models/models.yaml`, or a catalog of
  your own, is refused when it:
    - repeats a key;
    - has a top-level key other than `chat`, `embed` and `rerank`, such as a misspelled
      section, which before turned that family off without a warning;
    - has a `profiles:` section, which is removed;
    - sets `max_input_tokens` in an embedding model's `capabilities`, which is removed.

    `harborrag doctor` reports such a catalog in its `models catalog` check. Code that
    passes a request header whose name contains `key`, `token`, `auth`, `secret`,
    `signature` or `cookie`, such as `Idempotency-Key`, in a model request's
    `custom_headers` is refused unless the family's `security.allow_request_auth_headers`
    is on.
- **Keys under `runtime:` are checked.** `harborrag.yaml` and the SDK configuration file
  refuse a key under `runtime:` that is not a runtime setting, and the error names it.
  Before, a typo such as `contol_db_url` fell back to the default database. This covers
  the keys `harborrag.yaml` only exported as `HARBORRAG_*` variables, such as
  `local_source_root`, `log_level`, `connector_allowed_hosts`, `host`, `port`,
  `auth_mode` and the `mcp_*` keys, and the removed `memory_extraction_min_importance`
  and `memory_dedup_threshold`. Set those as environment variables instead, for example
  `HARBORRAG_LOCAL_SOURCE_ROOT`. `harborrag.yaml` matches keys in any case, the SDK file
  only in lower case. A project that the 2.0.0a2 `harborrag init` created uses only
  accepted keys.
- **Two settings have an upper limit.** A connection's `advanced.max_retries` takes at
  most 10, and `HARBORRAG_MEMORY_RETENTION_DAYS_SESSION`,
  `HARBORRAG_MEMORY_RETENTION_DAYS_USER` and `HARBORRAG_MEMORY_RETENTION_DAYS_PROJECT` at
  most 36,500 days. A retention above the limit stops the start-up. A stored connection
  above 10 is refused with `422 validation_error` when its next ingestion starts. A
  `config/sources/connectors.yaml` entry above 10 is imported with status `error`, and
  starting a run on it answers `422` the same way. `PUT` the connection with its whole
  `advanced` object and a `max_retries` of 10 or less: `PUT` replaces `advanced` whole.
- **The Qdrant gRPC port must match the URL.** While gRPC is preferred, as it is by
  default, the Qdrant client dialled port 6334 on the host of `HARBORRAG_QDRANT_URL`
  whatever port the URL named, so a stack on a mapped port could write vectors into
  another Qdrant. If `HARBORRAG_QDRANT_URL` names a port other than 6333, set
  `HARBORRAG_QDRANT_GRPC_PORT` to the gRPC port of the same Qdrant, or set
  `HARBORRAG_QDRANT_PREFER_GRPC=false`; otherwise the API, the worker, the MCP server
  and the CLI refuse to start. Set it wherever `HARBORRAG_QDRANT_URL` is set: with
  Compose, in `env/.env.temporal`, which Compose reads for the API and the worker. See
  [Ingestion runtime environment](configuration/config-file-reference.md#ingestion-runtime-environment).
- **The MCP audit directory must be owner-only.** `harborrag-mcp` now checks the
  directory of its durable audit file when it starts. It exits with
  `durable MCP audit directory must be owner-only (mode 0700)` when the directory grants
  any permission to its group or to other users, or with
  `durable MCP audit directory must be owned by this process user` when another user
  owns it. Before, the server started and failed every tool call. The directory is
  `~/.harborrag` unless `HARBORRAG_MCP_AUDIT_PATH` names another, and an earlier
  `harborrag auth keys create` or SharePoint sign-in may have created it with a mode
  such as 0755. Run `chmod 700` on the directory the message names, or set
  `HARBORRAG_MCP_AUDIT_PATH` to a file in an owner-only directory. HarborRAG now
  creates a missing `~/.harborrag` with mode 0700, but never changes the mode of an
  existing one.

### Upgrade the services in order

The API applies migrations `0041`–`0049` when it starts. They run as one transaction
that commits at the end, and nothing stops two API processes from migrating at the same
time. Workers never migrate. Several migrations lock busy tables until the run commits,
and only `0047`, `0048` and `0049` give up on Postgres after waiting 5 s for a lock:

| Migration | Change | Locks on Postgres |
| --- | --- | --- |
| `0041` | Turns every `sources` row into a connection | `ACCESS EXCLUSIVE` on `sources`, no timeout |
| `0042` | Adds the empty `connection_revisions` table | None |
| `0043` | Gives every project a version | `ACCESS EXCLUSIVE` on `projects`, no timeout |
| `0044` | Drops the `agent_runs` table | `ACCESS EXCLUSIVE` on `agent_runs`, no timeout |
| `0045` | Deletes the 1.x agent traces, drops their columns and rebuilds the `ix_memories_owner` index | Row locks for the deletes, then `ACCESS EXCLUSIVE` on each table it alters, no timeout |
| `0046` | Adds the runtime settings tables: the overrides and an append-only revision log | None |
| `0047` | Adds empty columns for a chat session's `project_id` and an answer's `mode`, `trace_id` and `feedback` | Brief locks on `conversation_sessions` and `conversation_messages`, 5 s timeout |
| `0048` | Removes the `DEFAULT` server default from `tenant_id` on nine tables | `ACCESS EXCLUSIVE` on each of them, 5 s timeout |
| `0049` | Clears raw secret values left in connection rows | Row locks on the rows it rewrites, 5 s timeout |

What the migrations change for stored data:

- `0045` deletes tool-call messages, replay records of agent sessions, and run-scoped and
  working memories, then drops the session `kind`, message `run_id` and usage `surface`
  columns. It deletes from `conversation_messages` in one statement. A downgrade brings
  the columns back, with `kind` and `surface` set to `chat` and the rest empty; the
  deleted rows do not come back.
- `0046` creates the tables behind [Runtime settings](configuration/runtime-settings.md),
  empty until a setting is changed or seeded.
- After `0047`, stored sessions and messages keep their IDs and read back with the new
  columns as `null`.
- `0048` covers `projects`, `sources`, `secrets`, `jobs`, `activity`, `providers`,
  `workspace_settings`, `members` and `graph_conflicts`, so the database no longer fills
  in a missing tenant; stored rows keep theirs.
- `0049` clears a credential that a connection still held as a raw value; see
  [Set cleared credentials again](#set-cleared-credentials-again). Alembic's offline
  `--sql` mode leaves its data step out, so SQL rendered that way and applied by hand
  clears nothing; let the API apply `0049` against the database.

Upgrade in this order:

1. Cancel any run that 2.0.0a2 paused while a batch was running; see
   [CLI, dependencies and Temporal](#cli-dependencies-and-temporal). Then stop the API,
   the MCP servers and every worker. Other runs in progress stay in Temporal and go on
   when the workers return. Do not start an inline run
   (`harborrag ingestions start --inline`) until the upgrade is done: it migrates the
   database as the API does.
2. Back up the control database. `0044`, `0045` and `0049` delete data that a downgrade
   does not bring back.
3. Start one API replica of the new release, and nothing else. Give its start-up and
   readiness probes enough time for the migrations: an orchestrator that restarts the
   API part way rolls the migrations back and starts them again. Wait until `/readyz`
   answers `200`.
4. Start the workers, all on the new release. The worker records the cancellation of a
   retry run, so with an older worker a retry run cancelled through the API ends
   `failed`, or stays `running` until the API next settles it, for example at the next
   start of its connection. If your workers are split by task queue, start the ones that
   serve `harborrag-io` no later than those that serve `harborrag-transform`: a 2.0.0
   document retry workflow gives its release a 2-minute heartbeat timeout, and an older
   `harborrag-io` worker never heartbeats, so every attempt times out. The shipped
   Compose worker serves every queue.
5. Start the MCP servers and the other API replicas.

Runs started before the upgrade replay on the new workers: the changed workflow code sits
behind seven Temporal patch IDs, `harborrag-source-records-cancellation`,
`harborrag-document-records-cancellation`, `harborrag-source-guarded-discovery-stop`,
`harborrag-source-cancel-closed-batch`, `harborrag-retry-heartbeat-timeout`,
`harborrag-document-retry-records-cancellation` and
`harborrag-retry-counts-cancelled-document`.

### After the upgrade

Once every service runs the new release, fix the connections first: set cleared
credentials and folder settings again, as the first two steps below show. Then start the
`force` re-ingest of every connection that
[Re-index when the embedding model changes](#re-index-when-the-embedding-model-changes)
asks for; it also covers the SharePoint step.

#### Set cleared credentials again

Migration `0049` removes any secret that a connection still held as a raw value. Such a
value came from a source row written before secret references existed, which `0041`
copied as it found it. The migration has no encryption key, so it cannot move the value
into the secrets store. Each such connection turns `error`; set the cleared fields again
before its next run. Most installs have none.

1. List the connections that need attention:

    ```bash
    harborrag connections list --status error
    ```

2. Find the cleared fields. While it migrates, the API logs one line per connection from
   the `harborrag.adapters.control_plane.migrations` logger, such as
   `Clearing raw connection secrets connection_pk=… tenant_id=DEFAULT fields=credentials.api_token`.
   The values are cleared when the migration commits. The fields are also in the
   connection's `status_detail`, which the API does not return. Read it in the control
   database:

    ```sql
    SELECT tenant_id, project_id, connection_id, status_detail
    FROM sources
    WHERE status = 'error';
    ```

3. Send each connection's whole body again with the cleared fields set. `--secret-prompt`
   asks for a credential without echoing it:

    ```bash
    harborrag connections update engineering/jira-main -f jira-main.yaml --secret-prompt api_token
    ```

    The update makes the connection `active` again.

#### Put back spaces in folder and glob settings

2.0.0a2 removed the spaces at the start and end of every connection setting. 2.0.0 keeps
them in folder and glob settings, because a file or folder name can start or end with a
space:

- local file connections: `source_path`, `include_paths`, `exclude_paths`,
  `include_globs`, `exclude_globs` and `excluded_dir_names`;
- GitHub connections: `root_path`, `include_paths`, `exclude_paths`, `include_globs` and
  `exclude_globs`;
- SharePoint connections: `root_path`.

A stored value has already lost its spaces, so saving the connection again as it is
changes nothing. If such a setting must start or end with a space, set it again with
the space in the body of `harborrag connections update`.

#### Re-ingest SharePoint connections once

SharePoint chunks now carry the document's `created_at`, `updated_at`, `author` and
`web_url`, and its site, drive and item IDs, in their vector payloads, so filters on them
work. A document gets these fields only when it is fetched again, and an incremental run
does not fetch a document that has not changed. Run one ingestion in `force` mode per
SharePoint connection:

```bash
harborrag ingestions start engineering/sharepoint-handbook --mode force --wait
```

The `force` re-ingest of every connection that
[Re-index when the embedding model changes](#re-index-when-the-embedding-model-changes)
asks for covers this step.

#### Expect a larger first run

Earlier releases left metadata out of the change check. A Jira issue whose assignee,
priority, status category, due date, resolved time, components, custom fields or update
time changed, or a Confluence page or local file whose descriptive fields or times
changed, was reported unchanged. Its payload filters kept the old values, and it was
fetched and parsed again on every run. 2.0.0 publishes a `METADATA_CHANGED` version for
each such document, which reuses its vectors and stores the new payload values, and a
local file touched in `stat` checksum mode gets a new version the same way.

The items earlier releases fetched on every run are those whose source change key moved
past their active version, and the first run after the upgrade gives each of them a new
version. Jira connections usually hold the most. Count them per connector type in the
control database:

```sql
SELECT d.connector_type, count(*) AS documents
FROM documents d
JOIN document_versions v ON v.document_version_id = d.active_document_version_id
JOIN source_items s ON s.document_id = d.document_id AND s.is_active
WHERE v.admission_change_key <> s.admission_change_key
GROUP BY d.connector_type
ORDER BY documents DESC;
```

Local files are fetched and parsed once on the first run after the upgrade, changed or
not. The record 2.0.0 keeps of each discovered local file adds the file's modification
time, so it differs from the record 2.0.0a2 stored. After that run, reading a file, for
example with a backup or `grep`, no longer makes the next run fetch it again: the change
check leaves out the file's access time, also in the records 2.0.0a2 stored.

The `force` re-ingest that
[Re-index when the embedding model changes](#re-index-when-the-embedding-model-changes)
asks for does this work too. From then on, a document fetched again and found unchanged
stores the change key its source reports, so the next run skips it without fetching.

#### Paths with unusual spacing scan again

No action is needed. A connection that ingested files whose paths hold a double space, a
tab or a no-break space, or start or end with a space, on 2.0.0a1 or 2.0.0a2, no longer
fails every run. Its first scan moves those source items to the document ID 2.0.0 gives
them.

### Update scripts for CLI v2.0.0

**Breaking:** the `harborrag` commands now follow the HTTP API. Each command calls the
same operation as its route, takes the same input, prints the same JSON and fails with
the same error codes; the [CLI reference](cli-reference/README.md) maps every command to
its route. The old names get no alias, so update every script, cron entry and runbook
that calls the CLI.

#### Rename the commands

An old subcommand, such as `harborrag source list`, prints the command that replaced it
and exits 2, with `--help` too:
`error: 'harborrag source list' was renamed; use 'harborrag connections list'`.
`harborrag source` and `harborrag ingest` alone name the group that replaced them.

| Before | Now |
| --- | --- |
| `harborrag source list` | `harborrag connections list` |
| `harborrag source show REF` | `harborrag connections get REF` |
| `harborrag source add REF --file BODY` | `harborrag connections create REF -f BODY` |
| `harborrag source update REF --file BODY` | `harborrag connections update REF -f BODY` |
| `harborrag source test REF` | `harborrag connections test REF` |
| `harborrag source sync REF` | `harborrag ingestions start REF` |
| `harborrag source remove REF` | `harborrag connections delete REF` |
| `harborrag ingest start CONNECTOR` | `harborrag ingestions start REF` |
| `harborrag ingest run CONNECTOR` | `harborrag ingestions start REF --inline`, which refuses a `local_file` connection whose `source_path` is neither a file nor a directory on this machine, such as the stack's `/data/sources` |
| `harborrag ingest status RUN_ID` | `harborrag ingestions get INGESTION_ID` |
| `harborrag ingest wait RUN_ID`, `harborrag ingest watch RUN_ID` | `harborrag ingestions get INGESTION_ID --watch` |
| `harborrag ingest pause RUN_ID`, `resume`, `cancel` | `harborrag ingestions pause INGESTION_ID`, `resume`, `cancel` |

These commands are removed, with no replacement in the CLI:

- `harborrag retrieve`: search with the MCP `vector_search` tool, or ask with
  `harborrag chat`.
- `harborrag topology` and every command under it, including
  `topology summaries backfill`, `topology config validate` and `apply`, and
  `topology indexing permissions-import`. The runtime services behind them remain, but
  2.0.0 has no HTTP route or MCP tool that exposes them.
- `harborrag init`, with its scaffold templates. A project directory an earlier `init`
  created keeps working through its `harborrag.yaml`, or with `--project DIR`. Set up a
  new install from a repository checkout, as the
  [Quick Start](../getting-started/quick-start.md) does.

New commands: `me`, `projects`, `source-types list`, `ingestions list` and
`retry-failures`, `failures list` and `retry`, and `settings schema`, `get` and `set`.

#### Name a connection by its ref

`ingestions start` and the `connections` commands take the connection's ref,
`PROJECT_ID/CONNECTION_ID`, where `ingest start` took its `source_key`. A connection
imported from `config/sources/connectors.yaml` sits in project `default` with its YAML
key as its ID, so `harborrag-workspace` becomes:

```bash
harborrag ingestions start default/harborrag-workspace --wait
```

A bare key is refused with exit 2 and a hint:
`Invalid value for REF: expected PROJECT_ID/CONNECTION_ID, for example default/harborrag-workspace; list them with harborrag connections list`.

The CLI no longer imports `config/sources/connectors.yaml` before its first ingestion.
The API imports it at its first start, and `ingestions start` needs the connection to
exist.

#### Replace the flags that changed

- `--json` is now `-o json`, and `-o yaml` is new. `-o json` prints the HTTP response
  body, not the `{"ok", "data", "error"}` envelope. Without `-o`, the CLI prints a table
  in a terminal and JSON otherwise.
- `--force-reprocess` is now `--mode force`. `--updated-after` and `--wait` stay.
- `ingest start` and `ingest run` options that scoped a run are removed with no
  replacement: `--connector-id` and `--connector`, `--run-id`, `--connection-id`,
  `--source-scope-id`, `--path`, `--pattern`, `--recursive` and `--no-recursive`,
  `--attachments` and `--no-attachments`, `--filters-json`, `--limit`, `--batch-size` and
  `--document-concurrency`. So are `--timeout`, `--refresh` and `--events`: Ctrl-C stops
  following a run.
- `source add --sync-now` is removed: run `ingestions start` after `connections create`.
- `source update --if-version N` is now `connections update --if-match ETAG`.
- `source list --type` is now `connections list --source-type`.
- `chat --session` and `--project` are now `--session-id` and `--project-id`. Without
  `--session-id`, `chat` asks a one-off question and creates no session.
- `config import` and `config export` are now `settings import` and `settings export`,
  with no alias: `harborrag config` exits `2` with `No such command 'config'`. Their
  arguments, options and output are unchanged.
- `config export --output PATH` is now `settings export --file PATH`; `-o` is the output
  format on every command.
- `doctor --temporal` and `doctor --json` are removed: `doctor` always checks Temporal,
  and `-o json` prints its report.
- `--tenant`, `-o`, `--profile`, `--config` and `-y`/`--yes` are global options, given
  after the command. A profile in `~/.config/harborrag/config.yaml` can set the default
  tenant and output; see [Global options](cli-reference/README.md#global-options).
- The CLI's tenant variable is `HARBORRAG_TENANT_ID`, as for the API and the workers; see
  [Rename the tenant variable](#rename-the-tenant-variable).
- From a checkout, the CLI reads `HARBORRAG_LOCAL_SOURCE_ROOT` from `env/.env.database`;
  see [Set the local source root](#set-the-local-source-root).

#### Read errors and exit codes again

Errors go to stderr: with `-o json` as the HTTP error envelope,
`{"error": {"code", "message", "details", "trace_id"}}`, and in a table as one line such
as `error 404 not_found: connection 'no-such' not found (trace 3877634d…)`. The exit
codes changed:

| Exit code | Before | Now |
| --- | --- | --- |
| `1` | Any failure, including a run that did not complete | Anything else, such as an internal error (`500`) |
| `2` | Usage error | Usage error or bad input (`400`, `413`, `422`, `501`); an invalid `settings import` file, which exited 1 |
| `3` | `--timeout` ran out | Not found (`404`) |
| `4` | none | Conflict (`409`, `412`), such as pausing a paused run, which exited 1 |
| `5` | none | Not allowed (`401`, `403`) |
| `6` | none | Unavailable (`429`, `503`, `504`); also `doctor` when only service checks fail |
| `8` | none | The run `--wait` or `--watch` followed ended `failed` or `cancelled`, or `connections test` reported `"ok": false` |
| `130` | Interrupted | Interrupted; the run keeps going |

`connections test` exits 8 when the check reports `"ok": false` and 0 when it passes. It
prints the same body either way, although the route answers `200`; `source test` exited 1
on a failed check.

#### Update scripts that read `auth keys` output

`harborrag auth keys create|list|revoke` printed `{"ok": true, "data": …}` and, on any
failure, `{"ok": false, "error_code": …}` with exit 1. It now prints like the other
commands:

- Success prints the object that was under `data`, such as `{"keys": [...]}` for `list`.
  Read `.keys` instead of `.data.keys`, and drop any check of `.ok`. `-o table`, `json`
  and `yaml` all work.
- A failure is the error envelope on stderr, `{"error": {"code", "message", …}}`, with
  the exit codes above: `2` for a policy violation, `3` for an unknown key, `4` when the
  database rejects the key row, and `6` when the key table is missing or behind. Branch
  on the exit code or `error.code`, not on `error_code`.
- `create` requires an explicit `--tenant` and exits 2 without it; it no longer falls
  back to `HARBORRAG_TENANT_ID`, the profile or `DEFAULT`. Only `list` keeps the CLI's
  default tenant, as [Global options](cli-reference/README.md#global-options) describes.
  A script that always passed `--tenant` keeps working. `revoke` still takes either
  `--key-id`, or `--tenant` together with `--owner`.

The secret still goes only to its `0600` file, never to the output.

#### Check what a write does

- `projects update` replaces the project body, as the `PUT` does: a field left out is
  cleared, so pass `--project-name` on every update.
- A write into a tenant that holds no data asks first, so a typo in `--tenant` does not
  create a tenant. Without a terminal it stops with exit 2 unless you pass `--yes`. This
  covers `settings import --apply` into a new tenant too.
- `connections delete` and `projects delete` ask before they remove anything, and need
  `--yes` without a terminal.
- Projects and connections the CLI writes, and the runs it starts through Temporal,
  record the CLI's principal, `cli:<os-user>`, in `created_by`, `updated_by` and
  `triggered_by`.
- The notice `harborrag: using repository checkout DIR` (or `using project DIR`) prints
  only when stderr is a terminal, so a script that reads stderr sees only errors.

### What happens at the first start

The API imports `config/sources/connectors.yaml` once, when all of these hold:

- the file exists;
- the install has no projects and no connections, in any tenant;
- no earlier import was recorded.

It imports into the tenant `HARBORRAG_TENANT_ID` names (`DEFAULT` unless set). An
install upgraded with projects or connections already stored, including rows carried
over from the old `/api/v1/sources` routes, is not imported: add its connections through
the API or `harborrag connections create`.

The import runs once. A failed import is recorded and not tried again; a project it
created that holds no connection is removed, and what it missed is added through the
API. An import stopped part way, for example by a restart, resumes at the next start.

Each entry becomes a connection:

- The entry's key becomes the connection's `source_key`, so its documents keep their ids
  and source scopes. The key is also its `connection_id` when it is a valid id
  (lowercase letters, digits, `-` and `_`); otherwise a slug of it is.
- `settings` and `environment` values are split into credentials and config. Environment
  values are read at import time.
- `secrets: <field>_env: NAME` becomes the reference `{"secret_ref": "env:NAME"}`. The
  value stays in the worker's environment. Such a reference keeps working; through the
  API, an editor can keep it or replace it with a value, but cannot point a field at
  another variable.
- Tuning such as `requests_per_minute` and `page_size` is kept on the connection and
  shown as its `advanced` settings.
- An entry with `project_id` goes to that project; others go to `default`, created when
  missing.
- A disabled entry is skipped. An entry with a missing environment variable or an
  invalid value is imported with status `error` and its field issues; fix it with `PUT`.
  A Jira or Confluence Cloud entry keeps the `api_token` method without its email, so
  the `PUT` only adds the `email`.

The plan is logged and the import is recorded in the workspace settings. The file is
never read again: later changes go through the API or the `harborrag connections`
commands. `harborrag doctor` keeps checking the file. The CLI does not run the import,
so start the API once before you start an ingestion from the CLI.

### Rows from `/api/v1/sources`

Existing rows become connections. Each gets a `connection_id` slugged from its name,
made unique in its project with `-2`, `-3` and so on, and the `source_key`
`<project_id>/<connection_id>`. A `paused` row becomes `active`; a row whose type is
unknown becomes `error`.

A Jira or Confluence row without an email gets the `pat` method, which a Cloud site
cannot use: starting its ingestion answers `422` on `credentials.email`. `PUT` the
connection with `"method": "api_token"` and the `email`; the stored token is kept.

### Behaviour that changes

#### Connections

- **SharePoint delegated sign-ins move under the HarborRAG tenant.** Without a
  `token_cache_path`, the refresh token now lives in
  `~/.harborrag/sharepoint/<harborrag-tenant>/token-<tenant>-<client>.json` instead of
  one file shared by every tenant. Sign in again with
  `scripts/deployment/dev.sh sharepoint-login` (or the `login` command with
  `--harborrag-tenant`) before the next sync; connections with an explicit
  `token_cache_path` are unaffected.
- **GitHub and SharePoint connections read only inside their `root_path`, and a run's
  filters can only narrow the connection's.** A SharePoint `tenant_id` must be a GUID or
  a domain name such as `acme.onmicrosoft.com`; `connections create`, `update` and `test`
  refuse anything else with a field error on `credentials.tenant_id`. Correct a stored
  value with `connections update`.
- **Connector configs built in Python take credentials only from their arguments.**
  `GitHubRepositoryConfig`, `ConfluenceSpaceConfig`, `JiraProjectConfig` and
  `SharePointSiteConfig` no longer read `GITHUB_TOKEN`, `CONFLUENCE_*`, `JIRA_*`,
  `MICROSOFT_*` or `HARBORRAG_INGESTION_TENANT_ID` when a value is missing. Pass each one
  explicitly, for example `token=os.environ["JIRA_TOKEN"]`. A GitHub config without a
  token reads public repositories anonymously; Jira, Confluence and SharePoint configs
  without credentials fail validation. Connections in `config/sources/connectors.yaml`
  and the control database are unaffected.
- **Configuration fingerprints change once.** Runs now check a fingerprint of the stored
  connection. Retrying the failures of a run started before the upgrade reports a
  configuration difference; start a new sync instead.
- **A change applies from the next run, on every worker, without a restart.** A run that
  is already going keeps the values it started with.
- **Editing a Jira connection's `jql` keeps its source scope**, so items the new query
  no longer matches are removed by the next full scan.
- **`GET /v1/admin/connections` lists stored connections** in the caller's tenant. The
  `source_type` for local files is `local_file`, and `POST /v1/admin/ingestions` starts
  one by its `project_id` and `connection_id`.
- **Request settings can be edited.** A connection's `advanced` object holds
  `requests_per_minute`, `page_size`, `request_timeout_seconds`, `max_retries` and
  `backoff_factor`; see [Advanced settings](connections.md#advanced-settings).
- **SharePoint takes a `page_size` from 1 to 100**, as Jira and Confluence do, and its
  default drops from 200 to 100. The 2.0.0a2 example `connectors.yaml` set
  `page_size: 200`: such an entry is imported with status `error`, and a stored
  connection with a value above 100 is refused when its next sync starts. `PUT` the
  connection with its whole `advanced` object and a `page_size` of 100 or less, or
  `null` for the default: `PUT` replaces `advanced` whole, and leaving it out keeps the
  refused value.
- **Every connector reads a fixed list of file types by default**: `md`, `markdown`,
  `txt`, `pdf`, `docx`, `doc`, `epub`, `odt`, `pptx`, `csv`, `xlsx`, `png`, `jpg`,
  `jpeg` and `html`. This covers Jira and Confluence attachments, which can now be
  filtered with `allowed_extensions` and `excluded_extensions`. A connection that does
  not set `allowed_extensions` stops reading other types, and its next full sync removes
  documents already read from them, such as source code in a GitHub repository. Set
  `"allowed_extensions": []` to keep every type, except on SharePoint, which needs at
  least one; see [File type filters](connections.md#file-type-filters).
- **Documents above 20 MiB are skipped by default.** A connection that sets
  `max_file_size_bytes` or `max_attachment_size_bytes` keeps its own value.
- **Parsers refuse oversized documents**: PDFs above 200 pages, images above 25
  megapixels, and spreadsheets above 50,000 rows or 200,000 cells. See
  [Document limits](connections.md#document-limits).
- **Connectors reach public addresses only.** A Jira or Confluence Data Center, GitHub
  Enterprise or other host on a private, loopback or link-local address is refused until
  its host name or network is listed in `HARBORRAG_CONNECTOR_ALLOWED_HOSTS`, in both
  `env/.env.connector` and `env/.env.api`. See
  [Network access](connections.md#network-access).
- **`env:` references come only from `connectors.yaml` and the CLI.** The HTTP API takes
  secret values and refuses a new `env:NAME`.
- **Local paths can be restricted.** With `HARBORRAG_LOCAL_SOURCE_ROOT` set, a
  `local_file` connection must stay inside it. Without it on the API, the API refuses
  new `local_file` connections; the CLI and `connectors.yaml` still accept them.
  **Breaking:** `LOCAL_SOURCE_PATH` no longer sets the root; see
  [Set the local source root](#set-the-local-source-root).
- **Discovery fails on a throttled or failing listing.** A SharePoint folder listing, or
  a selected item, that answers `429` or a `5xx`, or times out, now fails discovery, and
  Temporal retries discovery from its checkpoint. Before, the folder was skipped, and the
  next full scans retired the files below it. Only denied and deleted items are still
  skipped. Local discovery skips a directory it may not read, or one removed while it
  runs (skip reason `deleted`), and fails on any other listing error.
- **A document that stops yielding text leaves search.** When a file is emptied, or an
  attachment is replaced by an image-only scan, its active version is retired and its
  vectors and graph data are cleaned up. The run counts it as skipped with reason
  `no_indexable_content`.
- **Long Jira custom field names stay filterable.** A custom field whose normalized name
  is longer than 128 characters is filtered as `fields.customfield_<id>`, by its field
  ID. An issue keeps at most 128 typed fields in its payload; the rest are left out with
  a warning instead of failing the issue.
- **SQLite development databases enforce foreign keys.** Deleting a connection now
  removes its document versions too. Rows that an earlier release left behind stay.
- **Removed:** `GET /v1/connections` and `/api/v1/sources*` answer `404`.

#### Parser configuration

- **The API checks `config/ingestion/parsers.yaml` when it starts.** An invalid
  file stops the API with a configuration error, as it stops the worker. A
  `${VARIABLE}` reference without a default must now be set for the API as
  well, which does not receive `env/.env.parser` under Compose: set it in
  `env/.env.api`, or write `${VARIABLE:-}`. Without the file the API still
  starts and logs a warning.
- **Setting values are checked when the file loads.** A value outside what a
  setting accepts fails the load. Before, such a value failed only in the
  worker, was replaced by a default, or was passed on as written, as a limit
  written as a decimal or a boolean was. This covers the PDF `profile`,
  `min_content_chars` and `max_pages`, the image `ocr_engine`, `max_pixels`,
  `timeout`, `lang`, `config` and `ocr_server_url`, and the LiteParse
  settings. Two enabled definitions for one parser family fail the load as
  well. `harborrag doctor` reports the first error in its `parsers catalog`
  check; see
  [When the file is checked](configuration/parser-config.md#when-the-file-is-checked).
- **LiteParse `extra_options` cannot repeat a setting.** Move an argument
  that has its own key, such as `num_workers`, from `extra_options` to
  `settings`.

#### CLI, dependencies and Temporal

- **The CLI runs from a checkout.** Outside a checkout or a project directory, every
  command except `doctor` stops with `harborrag: no project found. …`. The command
  changes are in [Update scripts for CLI v2.0.0](#update-scripts-for-cli-v200).
- **`harborrag ingestions pause|resume|cancel` use the HTTP rules.** An action the run's
  status does not allow exits 4 with the `invalid_state` error and sends nothing to
  Temporal: pausing a paused run, pausing or resuming a retry run, resuming a running
  run that holds no pending pause, and any action on a finished run. A run the control
  database does not hold is not found (exit 3); stop such a run in the Temporal UI.
- **One run per connection, also inline and on a schedule.** `ingestions start --inline`
  refuses a connection that has a pending, running or paused run with
  `409 already_running` and exit 4, and names that run in `details.ingestion_id`, as the
  API does. A scheduled firing is skipped while the connection has such a run, not only
  while its scan runs.
- **`qdrant-client` must be 1.17 to 1.19.** Installs of the `qdrant` extra that pinned
  an older client need `pip install -U "qdrant-client>=1.17,<1.20"`.
- **A paused run shows as `Running` in the Temporal UI.** HarborRAG now pauses a run by
  signal only; its own status still reads `paused`. Do not pause a run from the Temporal
  UI or CLI on Temporal 1.31.x: that native pause can strand the run. Native pause is
  safe only on Temporal 1.32 or later together with `temporalio` 1.35 or later.
- **Cancel runs that an earlier release paused mid-batch before you upgrade.** A run
  started before the upgrade that was paused while a batch was running keeps the old
  native pause on its later pauses, until it continues as new (every 25 batches by
  default), so pausing it again can still strand it. Cancel such runs before you
  upgrade, or clear them as below if one hangs.
- **Clear a run that hangs after a pause.** When resume and cancel change nothing,
  terminate the run's `harborrag-source:<ingestion_id>` workflow first, then its
  `harborrag-source-batch:<ingestion_id>:<n>` child, in the Temporal UI. Terminating the
  child first fails the run, and cancelling it then answers `409 invalid_state`.
  HarborRAG records the run as `cancelled` when you next cancel it or start an ingestion
  of its connection.

#### Logger names

- **Loggers that moved with their code are renamed.** A log filter, alert rule or
  dashboard query that selects records by logger name needs the new name. Messages and
  levels are unchanged. Each logger is now named after the module that owns it, so a
  prefix that covered several modules becomes one name per module.

    | Old logger | New logger |
    | --- | --- |
    | `harborrag.app.api.chat` | `harborrag.runtime.application.chat.replay` |
    | `harborrag.app.api.readiness` | `harborrag.app.operations.readiness` |
    | `harborrag.app.workflow_control.chat` | `harborrag.runtime.application.chat.service` |
    | `harborrag.app.workflow_control.chat.query_scope` | `harborrag.runtime.application.chat.query_scope` |
    | `harborrag.app.workflow_control.composition.*` | `harborrag.runtime.application.composition.*` |
    | `harborrag.app.workflow_control.control_plane.*` | `harborrag.runtime.application.control_plane.*` |
    | `harborrag_app.workflow_control.control_plane.settings_import` (named by module, outside the `harborrag` root) | `harborrag.runtime.application.control_plane.settings_import` |
    | `harborrag.app.workflow_control.ingestion` | `harborrag.runtime.application.ingestion.service` and `harborrag.engine.ingestion.failure_inbox` |
    | `harborrag.app.workflow_control.ingestion.connection_sources`, `.direct` and `.progress_bridge` | the same names under `harborrag.runtime.application.ingestion` |
    | `harborrag.app.workflow_control.ingestion.durable`, `harborrag.app.workflow_control.memory.extraction` and `harborrag.app.workflow_control.retrieval` | removed with their modules |
    | `harborrag.app.workflow_control.memory` | `harborrag.runtime.application.memory.composition` and `.context`, `harborrag.memory.usage`, `harborrag.memory.messages` and `harborrag.memory.titles` |
    | `harborrag.app.workflow_control.memory.administration` | `harborrag.memory.administration` and `harborrag.memory.erasure` |
    | `harborrag.app.workflow_control.scheduling` | `harborrag.runtime.application.scheduling.service` |
    | `harborrag.runtime.ingestion.capture` | `harborrag.engine.ingestion.release.capture` |
    | `harborrag.runtime.ingestion.cleanup` | `harborrag.engine.ingestion.maintenance.cleanup` |
    | `harborrag.runtime.ingestion.document_pipeline` | `harborrag.engine.ingestion.release.pipeline` |
    | `harborrag.runtime.ingestion.reindex` | `harborrag.engine.ingestion.maintenance.reindex` |
    | `harborrag.runtime.ingestion.version_lifecycle` | `harborrag.engine.ingestion.release.lifecycle` |
    | `harborrag.runtime.retrieval` | `harborrag.engine.retrieval.service.service`, `harborrag.engine.retrieval.service.graph_observation` and `harborrag.engine.retrieval.service.result_loader` |
    | `harborrag.runtime.retrieval.permissions` | `harborrag.engine.retrieval.service.permissions` |
    | `harborrag.runtime.retrieval.topology` | `harborrag.engine.retrieval.service.topology` |
    | `harborrag.runtime.tools.*` | `harborrag.engine.tools.<module>` (`documents` is `document_tools`, `entities` is `find_entities`, `readers` is `reader_tools`, `sources` is `source_list_tool`; `vector_search` keeps its name; `graph_search` splits into `graph_search` and `graph_search_support`) |

#### File names in logs

- **Logs and errors name a file by a digest.** Parser log lines and parser errors no
  longer contain a file name or path. They label the input as
  `input=DIGEST suffix=SUFFIX size=SIZE`, where `DIGEST` is the first 12 hex characters
  of the SHA-256 of the file name. Local and GitHub connector logs, and local load
  errors, name a path by the same kind of digest of the path. To find the lines for a
  file, compute its digest:

    ```bash
    printf '%s' 'design-review.pdf' | sha256sum | cut -c1-12
    ```

- **Temporal history keeps a failure's code, not its text.** An ingestion activity
  failure no longer carries the original error, which could quote a file name or a
  signed download URL; it keeps the failure's safe code. A PDF or OCR engine failure
  that a library raised is named by its exception class instead of the library's
  message.

#### Python import paths and service methods

These changes affect code that imports HarborRAG, such as an SDK caller or a worker
extension. They do not affect the HTTP API, the CLI or the MCP server. The packages keep
no alias at an old path: an old import raises `ModuleNotFoundError` or `ImportError`, and
a script that used one must import from the new path. Rewrite an import by finding the
old package in the first column and applying the pointer rule.

| Old import path (2.0.0a2) | New import path | Pointer rule |
| --- | --- | --- |
| `harborrag_adapters.connectors.schemas` | `harborrag_core.ingestion.connector_contracts` | `ConnectorQuery`, `ConnectorSkip`, `ConnectorMetadata` and `ConnectorCapabilities` are no longer importable from `harborrag_adapters.connectors` |
| `harborrag_adapters.topology.{evidence,output_policy}` | `harborrag_core.topology.<module>` | `LLMEntityExtractor` stays in `harborrag_adapters.topology` |
| `harborrag_app.api.auth.principal` | `harborrag_app.operations.principal` | |
| `harborrag_app.api.*` request and response models | `harborrag_app.operations.schemas.<resource>` | One module per resource: `chat`, `connections`, `ingestions`, `me`, `settings` and `common`. The models changed with the HTTP API, so check each field |
| `harborrag_app.workflow_control` | `harborrag_runtime.application` | Keep the rest of the path: `workflow_control.chat.service` is `application.chat.service`, and `AppResponse`, `BaseAppService` and the `AppService` composition follow the same rule. `harborrag_app` no longer exports `AppResponse` or `BaseAppService`. `chat.events`, `chat.stream_result` and `chat.turn` are removed |
| `harborrag_app.workflow_control.chat.memory_block` | `harborrag_engine.chat.memory_block` | Chat modules stay in `application.chat`, except this one |
| `harborrag_app.workflow_control.ingestion.{models,presenters,recovery}` | `harborrag_engine.ingestion.{models,presenters,recovery}` | The failure inbox is `harborrag_engine.ingestion.failure_inbox` |
| `harborrag_app.workflow_control.ingestion.progress_bridge.event_prefix` | `harborrag_core.contracts.events.event_prefix` | The rest of `progress_bridge` is `harborrag_runtime.application.ingestion.progress_bridge`. Stored and bus event names are unchanged |
| `harborrag_app.workflow_control.memory.{access,administration,erasure,messages,projects,titles,usage,locks,presenters,directory}` | `harborrag_memory.<module>` | `ModelCall`, `record_model_usage`, `SessionLocks`, `ConversationTurnBusyError`, `ConversationDirectoryService` and the presenter functions keep their names, and `harborrag_runtime.application.memory` no longer re-exports them. The error classes keep their `error_code` and HTTP status. Session and context modules stay in `harborrag_runtime.application.memory`; `extraction` is removed |
| `harborrag_app.workflow_control.memory.identity.MemoryIdentity` | `harborrag_core.domain.identity.MemoryIdentity` | `harborrag_runtime.application.memory` no longer exports it |
| `harborrag_engine.ingestion.policies` | `harborrag_core.ingestion.policies` | |
| `harborrag_runtime.ingestion.document.{models,stage_models,capture_decisions,materialization_helpers,projection_coordinator}` | `harborrag_engine.ingestion.release.<module>` | `DocumentReleaseRequest` and `DocumentReleaseOutcome` are in `release.models`; `harborrag_runtime.ingestion` and `.ingestion.document` no longer re-export them |
| `harborrag_runtime.ingestion.document.{lifecycle,dependencies,projection_material,materialization,projection}` | `harborrag_engine.ingestion.release.<module>` | `DocumentReleaseDependencies` is in `release.dependencies`, and `harborrag_runtime.ingestion` and `.ingestion.document` no longer export it. It now requires a `failures` classifier, and `parser_for` and `chunker_for` callables replace its `run_tools` field. `DocumentVersionLifecycle` requires `failures` too. `run_tools` stays in `harborrag_runtime.ingestion.document` |
| `harborrag_runtime.ingestion.document.{capture,preparation,pipeline,service}` | `harborrag_engine.ingestion.release.<module>` | Connector arguments are typed `harborrag_engine.ingestion.release.dependencies.RawDocumentLoader`, a structural type with one method, `load(record) -> RawDocument`, which every connector satisfies. The pipeline reads its stage order from `harborrag_core.ingestion.stages.DOCUMENT_STAGE_ORDER`. `DocumentReleaseService` is no longer exported from `harborrag_runtime.ingestion`, and neither it nor `DocumentStagePipeline` is exported from `.ingestion.document` |
| `harborrag_runtime.ingestion.document.normalizers.{builder,router}` | `harborrag_engine.ingestion.normalizers.<module>` | `builtins` and `transform` stay in `harborrag_runtime.ingestion.document.normalizers` |
| `harborrag_runtime.ingestion.maintenance.{reindex,reindex_reuse}` | `harborrag_engine.ingestion.maintenance.<module>` | `DocumentReindexService`, `ReindexRequest` and `source_identity_from_canonical` are no longer exported from `harborrag_runtime.ingestion`; import them from `harborrag_engine.ingestion.maintenance.reindex` |
| `harborrag_runtime.ingestion.maintenance.{cleanup,reindex_plan,relation_supersession}` | `harborrag_engine.ingestion.maintenance.<module>` | |
| `harborrag_runtime.ingestion.source.{models,tasks}` | `harborrag_engine.ingestion.source.<module>` | `SourceIngestionRequest` and `SourceIngestionOutcome` are in `source.models` |
| `harborrag_runtime.ingestion.limits`, `harborrag_runtime.source_query` | `harborrag_core.ingestion.limits`, `harborrag_core.ingestion.source_query` | `SourceQuery` and `ProcessingProfileInput` are no longer importable from `harborrag_runtime.temporal.schemas`, and `SourceSubmission` not from `harborrag_runtime.temporal.submission` |
| `harborrag_runtime.ingestion_contracts.{SourceSubmission,PreparedSourceSubmission,IngestionRetryRequest}` | `harborrag_core.ingestion.run_contracts` | `harborrag_runtime.ingestion_contracts` keeps only the execution gateway types |
| `harborrag_runtime.memory.in_memory` | `harborrag_memory.tiers.in_memory` | |
| `harborrag_runtime.retrieval.<module>` | `harborrag_engine.retrieval.service.<module>` | Applies to every module except `facades` and `composition`, which stay, and `knowledge`, which is removed; `service` is `harborrag_engine.retrieval.service.service`. `RuntimeRetrievalService.connect` is removed: call `connect_retrieval_service(settings)` from `harborrag_runtime.retrieval.composition`. `RetrievalOptions`, `RetrievalPolicy`, `RetrievalResources` and the other types of `contracts` are in `harborrag_engine.retrieval.service.contracts`; `harborrag_runtime.retrieval` no longer re-exports them |
| `harborrag_runtime.tokenization` | `harborrag_core.chunking.tokenization` | |
| `harborrag_runtime.topology.{derived_cleanup,inspection,reservation_deadline}` | `harborrag_engine.topology.<module>` | |

Removed from `BaseAppService` (`harborrag_runtime.application.ports`; `AppService` loses
them too). The other methods that 2.0.0a2 had on it for projects, sources, settings,
metrics, graph conflicts, projections, agent sessions and `ingest_once` are gone as well,
with the routes they served. The replacements are methods of `AppService`; of them,
`BaseAppService` declares only `get_task`.

| Removed method | Call instead |
| --- | --- |
| `start_ingestion(...)` | `submit(command, idempotency_key=...)` with a `harborrag_engine.ingestion.models.IngestionCreateCommand`; it registers the run and starts its workflow |
| `ingestion_status(run_id)` and `ingestion_result(run_id)` | `get_task(task_id)`, which returns the run's public task document and raises `IngestionNotFoundError` for an unknown run |
| `control_ingestion(run_id, action)` | `pause(task_id)`, `resume(task_id)` or `cancel(task_id)`, one method per action |
| `retrieve(query, ...)` | `HarborRAG().retrieval.search(RetrievalRequest(...))` of `harborrag_runtime.sdk`, or `harborrag_engine.retrieval.service.service.RuntimeRetrievalService.retrieve` |

**Breaking:** `AppService.resume(task_id)` on a running run now resumes it when its
workflow holds a pause it has not applied yet, such as one sent during discovery. On a
running run with no pending pause it raises `IngestionActionStateError`
(`invalid_state`) instead of `IngestionAlreadyRunningError` (`already_running`). Catch
the new error, or the `HarborConflictError` both derive from.

These had no production caller and are removed with no replacement. An import of one
raises `ModuleNotFoundError`, `ImportError` or `AttributeError`.

| Removed | Notes |
| --- | --- |
| `HarborRAG().knowledge.fetch_evidence`, `resolve_entities`, `lookup_entities`, `find_relations` and `find_paths` | `HarborRAG().knowledge` keeps `list_documents`, `read_evidence`, `get_document_context` and `list_sources` |
| `RuntimeRetrievalService.fetch_evidence`, `resolve_entities`, `lookup_entities`, `find_semantic_relations` and `find_semantic_paths`, and `harborrag_engine.retrieval.service.knowledge` | |
| `AppService.chat_stream` and `BaseAppService.chat_stream`, with `harborrag_runtime.application.chat.{turn,stream_result,events}` | Call `chat_completion()` or `chat_answer()`. The SDK model-level stream, `HarborRAG().chat.stream`, stays |
| The chat presenters `evidence_data`, `cited_evidence`, `chat_response_data`, `chat_stream_chunk_data` and `source_heading`, `prompting.EVIDENCE_PREAMBLE`, and `MEMORY_WARNING` and `STREAM_ERROR` of `chat.service` | |
| `harborrag_runtime.application.memory.extraction` (`MemoryExtractionQueue`, `submit_exchange`, `drain_extraction_queue`, `dropped_extractions`), and `AppService.start_memory_extraction` and `drain_memory_extraction` | Nothing queued work for it. `HARBORRAG_MEMORY_EXTRACTION_MIN_IMPORTANCE` and `HARBORRAG_MEMORY_DEDUP_THRESHOLD` are ignored. `HarborRAG().memory.extract` and `HARBORRAG_MEMORY_EXTRACTION_ENABLED` stay |
| `harborrag_adapters.repositories.cache`, `harborrag_adapters.repositories.state` and `harborrag_adapters.repositories.database.{sqlalchemy,postgresql,sqlite,client,base}` | `harborrag_adapters.repositories.database` exports only the ingestion control plane: `ChunkRepository`, `DocumentRepository`, `HarborDatabaseBackend`, `HarborUnitOfWork`, `HarborUnitOfWorkFactory`, `OutboxRepository`, `HarborDatabaseClient` and the `PostgreSQLDatabase*` and `SQLiteDatabase*` classes are gone. Their tables were never migrated, so nothing changes in the database |
| `harborrag_runtime.ingestion.{connector_metrics,discovery_metrics,observability_types}` | Import the same names from `harborrag_runtime.observability.<module>` |
| `harborrag_adapters.parsers.registry.HarborParserEngineRegistry` | Use `HarborParserRegistry` |
| `harborrag_adapters.parsers.pdf.engines.<engine>.mapping` | Import the helpers from `harborrag_adapters.parsers.pdf.normalization` |
| `harborrag_app.api.auth.dependencies.authorize_tenant` and `authorize_task_tenant`, `harborrag_app.operations.principal.Principal.tenant_scope`, and `harborrag_app.operations.schemas.ingestions.IngestionStatus` | |
| The model catalog's `profiles:` section, the `--profile` option of `python -m harborrag_adapters.models`, and the `profile` argument of `HarborChatClientConfig`, `HarborEmbedClientConfig` and `HarborRerankClientConfig` `from_file` and `from_dict` and of `prepare_config_section` | A catalog with `profiles:` is refused |
| `HarborEmbedCapabilities.max_input_tokens` | A catalog that sets it is refused |
| `harborrag_adapters.models.runtime.litellm_telemetry` and `LiteLLMTelemetryCallback` | |
| `ConnectionImportService.export` and `importing.EXPORT_VERSION` of `harborrag_runtime.config.connectors` | |
| `scripts/models/download_docling_models.py`, `download_fastembed_models.py` and `warmup_models.py`, `scripts/generate_provider_matrix.py` and `make provider-matrix` | The model scripts were empty |

Code that implements `DocumentVersionPort` must add `refresh_admission_change_key`, and
a caller of `active_relation_document_ids` must pass `tenant_id`.

#### MCP server built in Python

`harborrag-mcp` and `python -m harborrag_mcp_server` read `HARBORRAG_MCP_AUDIT_PATH` once
at launch and pass the path to the server. Code that builds `McpServer(...)` itself, or
calls `create_mcp_server()` without a registry, no longer reads the variable: it writes
the audit log to the default `.harborrag/mcp-audit.jsonl`. Pass
`audit=McpAuditLog(path=...)` from `harborrag_mcp_server.audit` to choose the path; see
[Setup and integration](detailed-guides/mcp-server/setup-and-integration.md#use-from-python).

#### Connectors built outside the runtime

The runtime reads `HARBORRAG_CONNECTOR_ALLOWED_HOSTS` and `HARBORRAG_CONNECTOR_USE_PROXY`
once, when it composes the API or an ingestion runtime, and hands the resulting
`DestinationPolicy` to the connectors that ingestion builds: workers, inline runs such as
`ingestions start --inline`, and SDK direct mode. The connection test (`connections test`
and its API route) uses the same policy. A connector built from a config file with
`ConnectorDefinition.build` or `ConnectorCatalog.build`, or with `build_connector` or
`build_connector_from_parts` and no `destination_policy`, gets the strict default.
Connectors no longer read these variables when they open a session, and
`guarded_session` requires a policy. A GitHub, Jira, Confluence or SharePoint connector
that your own code builds with `HarborConnector` or the connector class, without the
runtime, now reaches public addresses only, whatever the environment says. To reach a
private host, pass `destination_policy=DestinationPolicy.from_environ()`, or a policy you
build, from `harborrag_adapters.connectors.policies.network`. The deployment's
environment variables and their effect do not change.

#### MCP server

- **MCP tools answer with empty results until `env/.env.mcp` shares the corpus.**
  The shipped template now sets `HARBORRAG_CORPUS_ACCESS_MODE=tenant_shared` and
  `HARBORRAG_CORPUS_SHARED_TENANT_ID=DEFAULT`, as the API and worker templates do.
  An existing `env/.env.mcp` keeps `source_acl` until you add both lines and
  restart the MCP server.
- **The HTTP server checks the `Host` and `Origin` headers.** A request
  addressed to a name other than `127.0.0.1`, `localhost` or `::1` now gets
  `421`, and one sent from another origin gets `403`. If a reverse proxy
  forwards its own host name to the loopback listener, list that name in
  `HARBORRAG_MCP_ALLOWED_HOSTS`, use reader keys
  (`HARBORRAG_MCP_AUTH_MODE=api_key`), and restart the server.
- **The MCP containers join the data network.** `scripts/deployment/mcp.sh`
  and `mcp-ui.sh` no longer use the host network. The container reaches the
  data services by name on `harborrag-data-network`, and Docker publishes the
  HTTP port on `127.0.0.1`. Docker Desktop host networking is no longer
  needed, and `HARBORRAG_QDRANT_PREFER_GRPC=false` can leave `env/.env.mcp`.
  Start the data services first with `scripts/deployment/dev.sh data`; only
  `--check` runs without them. The containers start only with `HARBORRAG_ENV=dev`,
  because the data services there use no TLS. Replace a running server with
  `scripts/deployment/mcp.sh down` and
  `scripts/deployment/mcp.sh --build --http`.
- **Each credential is rate-limited.** A reader key or bearer token gets at most
  `HARBORRAG_MCP_RATE_LIMIT` requests per second (default 10) and
  `HARBORRAG_MCP_MAX_INFLIGHT` requests at once (default 4), shared by `/mcp` and
  `/api/tools`. A refused `/mcp` request gets the JSON-RPC error `-32000`, and
  `/api/tools` answers `429` with `Retry-After`. Each server process keeps its own
  budget, so the reader server and the Explorer UI server count separately. The status
  page, `/healthz` and stdio are not limited. A client that sends more, such as a
  script looping over `vector_search`, is now refused: raise the two variables in
  `env/.env.mcp`, or set one to `0` to turn that limit off, and restart the server. A
  value that is not `0` or a positive whole number stops the launcher.
- **Reader tools show only what the caller may read.** Under `source_acl`,
  `list_sources` counts documents and reports the last ingestion time from the documents
  the caller may read, not the whole source, and `resolve_graph_nodes` shows nodes the
  tenant owns to any reader of the tenant. A tool error returns its text only when the
  tool's arguments were wrong; a backend error that could quote stored content is
  opaque.
- **`scripts/deployment/mcp.sh --http` checks the configuration first.** It
  starts nothing when a setting or backend is missing, and says which one; see
  [Checks before the HTTP server starts](detailed-guides/mcp-server/setup-and-integration.md#checks-before-the-http-server-starts).
- **Rebuild the MCP image once.** Run
  `scripts/deployment/mcp.sh --build --check` so the image records the commit
  it was built from. The wrapper then says when the image is older than the
  source.
- **The status page cannot save in the container.** `config/` is mounted
  read-only there, so **Save** is disabled and `PUT /api/config` answers `409`.
  Edit `config/mcp/server.yaml` and select **Reload YAML**.

### HTTP API changes

#### Routes, errors and limits

- **Removed routes answer `404`, with no deprecated alias:** every `/api/v1/*` route,
  including `/api/v1/health`, `/api/v1/readyz` and `/api/v1/metrics`, and
  `/v1/connections`, `/v1/agent/*`, `/v1/retrieval/*`, `/v1/graph/*`, `/v1/memory/*`,
  `/v1/schedules/*`, `/v1/admin/projections/*` and
  `PATCH`/`DELETE /v1/chat/sessions/{session_id}`. MCP server tools replace agent and
  retrieval access.
- **Probes and docs moved to the root:** `/healthz`, `/readyz`, `/docs` and
  `/openapi.json`.
- **`/metrics` answers on the ops port** (`HARBORRAG_OPS_PORT`, 9465), only to a bearer
  token equal to `HARBORRAG_METRICS_SCRAPE_TOKEN`. A blank token refuses every scrape
  with `401`; see
  [Check the settings that are now refused](#check-the-settings-that-are-now-refused).
  The API image starts with `python -m harborrag_app.api.serve`.
- **A Qdrant gRPC or S3 outage answers `503 dependency_unavailable`** instead of `500`.
  An S3 request is tried at most twice, with 10 s to connect and 30 s to wait for data
  each time, and then fails as a storage timeout that ingestion retries.
- **A project `PUT` that changes nothing keeps the project's version.**
  `PUT /v1/admin/projects/{project_id}` with the stored body keeps the version and the
  `ETag` and records no activity entry, so repeating it with `If-Match` no longer
  answers `412`.
- **Every error uses one catalog of codes**, such as `not_found`, `validation_error`,
  `unauthenticated` and `rate_limited`, in place of codes like `harbor_not_found_error`.
  `details` is `null`, an object, or for `validation_error` a list of `{field, issue}`.
- **The trace header is `X-Trace-Id`**, 32 lowercase hex characters, in place of
  `X-Request-Id`.
- **Each principal is rate-limited**: `HARBORRAG_HTTP_RATE_DEFAULT` (10) calls per
  second on `/v1`, and `HARBORRAG_HTTP_RATE_CHAT` (2) chat completions per second,
  answering `429` with `Retry-After`.
- **A credential with several tenants sends `X-Tenant-Id`** on admin routes, else
  `400 tenant_required`; a tenant it does not hold is `403 tenant_forbidden`.
- **Identifiers are named after what they identify**, never a bare `id`, and the old
  names are not accepted:
    - a project's `id` is `project_id`, and its `name` and `description` are
      `project_name` and `project_description` in the responses and the `PUT` body.
    - a connection's `ref` is `connection_ref` and its `last_ingestion.id` is
      `last_ingestion.ingestion_id`; the reader filter chips return `connection_ref`.
    - the started ingestion returns `ingestion_id`, its `connection` ref is split into
      `tenant_id`, `project_id` and `connection_id`, and `triggered_by` is
      `{principal_type, principal_id}`. `workflow_id` and `run_id` keep their names.
    - a source type's `auth_methods[].id` is `auth_method_id`, and a connection test's
      `preview.sample[].ref` is `item_ref`.
    - `GET /v1/me` returns `principal.principal_type` and `principal.principal_id`.
    - a chat completion returns `completion_id`.
    - a project delete refused with `409` names `details.connection_refs`, or
      `details.connection_ref` for a connection that is ingesting.

#### Access under `source_acl`

These changes apply to a tenant in `source_acl` mode. A tenant in `tenant_shared` mode
reads and lists everything, as before.

- **An API key reads documents as its owner.** Over HTTP, the document ACL now checks an
  API key's owner, as the MCP server already did. A grant to the owner reaches the key's
  chat answers, and an explicit deny for the owner hides documents from them. Before,
  HTTP checked the key ID, which holds no grants, so the key read only sources that need
  none. Chat sessions and `Idempotency-Key` replay are keyed by the key's owner, not the
  key ID. `/v1/me` and rate and capacity limits still use the key ID. User tokens are
  unchanged.
- **The connection chips list only connections the caller can search.**
  `GET /v1/projects/{project_id}/connections` lists only connections with a source the
  caller may read. A connection that was never ingested has no source yet and is hidden.
  A caller who may read more sources than the permission check can list (10,000) gets
  `409`, as retrieval does. Each listed connection's `documents` count is still the
  connection's total, not the number of documents the caller may read.

#### Ingestions and failures

- **Ingestions start at `POST /v1/admin/ingestions`** with `project_id` and
  `connection_id` in the body. `POST …/connections/{connection_id}/sync`,
  `POST /v1/ingestions` and the `sync_now` field of connection `PUT` are removed.
- **Ingestion runs moved to `/v1/admin/ingestions`:** `/v1/ingestions*` answers `404`;
  `{task_id}` is now `{ingestion_id}` and `/stream` is `/events`. The responses use the
  `Ingestion` shape described below.
- **The ingestion event stream uses new event names, with no alias.**
  `task.<id>.progress` and `task.<id>.done` are gone. `run.status`
  (`{status, counters}`, lowercase status; sent when a pause or resume changes the
  status, and covering `queued`, `running` and `paused`), `stage.progress`
  (`{stage, done, total}`), `document.failed` (`{document_id, stage, error_category}`)
  and `run.completed` (`{counters, finish_reason}`, where `finish_reason` is the final
  status, so a cancel arrives here) replace them, and the stream ends after
  `run.completed`. Each frame's `id` is an integer that increases; `task.<id>.control`
  events are no longer streamed. Events a run stored before the upgrade under the old
  names stay stored but are not streamed, and a run that ended before the upgrade gets
  only a `run.completed` the first time it is read, with no `document.failed` backfill.
  A reconnect to an ended run with nothing left after its `Last-Event-ID` answers `204`.
  Clients that matched the old names must switch to the new ones.
- **Ingestion runs answer the explorer `Ingestion` shape**, and the task shape
  (`progress`, `stage`, `source`, `submitted_at`, `message`) is retired with no alias. A
  run carries `ingestion_id`, `tenant_id`, `project_id`, `connection_id`, `mode`, a
  lowercase `status` (`queued`, `running`, `paused`, `succeeded`, `failed`,
  `cancelled`), `counters`, `error_category`, `triggered_by`, `settings_revision`,
  `workflow_id`, `run_id`, `trace_id`, `links`, `created_at` and `finished_at`.
- **Ingestion reads need role `editor` and send the tenant in `X-Tenant-Id`.** The
  `?tenant_id` query parameter is removed; a run in another tenant is `404`. The list
  filters are `project_id`, `connection_ref`, `status` and `created_after`.
- **Pause, resume and cancel answer `200` with the run** after waiting for the new
  status; an action the state does not allow is `409 invalid_state`. `resume` also
  accepts a `running` run whose workflow holds a pause it has not applied yet, such as a
  pause sent during discovery, and takes the pause back; on a `running` run with no
  pending pause it is `409 invalid_state`. A retry run can be cancelled but not paused or
  resumed: `pause` and `resume` on it are `409 invalid_state`. `retry-failures` answers
  `202` with the new run and its `Location`.
- **`counters.discovered` and `counters.skipped` include items skipped at discovery**,
  so `processed + skipped + failed` reaches `discovered` when every item is done. The
  CLI's run tables add a `Skipped` column.
- **Some failure codes changed.** Update failure-inbox filters, alert rules and
  dashboards that match on `error_category`:
    - a document run cancelled from Temporal, through its UI, its CLI or a cancelled
      schedule, is recorded as cancelled, not failed, and its code is
      `document_release_cancelled` instead of `document_release_cancellederror`;
    - a request to a private address the operator has not allowed is
      `document_release_destination_blocked`, an invalid configuration, and is not
      retried;
    - a write that Qdrant or S3 rejects with `400` is
      `document_release_storage_request_rejected`, an invalid configuration, and is not
      retried in the run. A failure-inbox retry still includes it, so you can retry after
      fixing the configuration;
    - a GitHub permission `403` is `document_release_authorization_failed` and a GitHub
      `404` is `document_release_source_item_not_found`; a secondary rate limit is still
      retried;
    - an HTTP `4xx` other than `408` and `429`, an oversized response, a redirect to
      another origin and a chained redirect fail without retry;
    - the `EXTRACTION_FAILED` count of a run's `skipped_by_reason` covers only parser
      rejections (`parser_rejected_document` and `document_unsupported`), no longer
      outages, a missing parser or cancellations at the parse stage.
- **Failure inbox contract:** `GET /v1/admin/failures` leaves out `total` unless you
  send `include_total=true`, instead of answering `null`.
  `POST /v1/admin/failures/retry` answers `409 invalid_state` when no failure matches,
  instead of `202` with `ingestion_id: null`.
- **Connection `stats.errors`** is the failed-document count of the latest run, the same
  number as `counters.failed` of that run, and `0` for a connection that never ran; it
  was `null` after a cancelled run.
- **New routes:** `GET /v1/admin/failures` and `POST /v1/admin/failures/retry` for the
  failure inbox; see
  [Review and retry failures](connections.md#review-and-retry-failures).
- **New settings:** `HARBORRAG_TEMPORAL_UI_URL` and `HARBORRAG_GRAFANA_URL` fill `links`
  of a run; add them to `env/.env.api`.

#### Settings schema

- `GET /v1/admin/settings/schema` answers
  `{"groups": [{"name", "title", "settings": [...]}]}` instead of a map by key. Each
  setting carries `key`, `type`, `default`, `levels`, `applies` and `description`, plus
  `min` and `max` for a bounded key and `enum` for an enumerated key.

#### Chat

- **Chat sessions and history answer the explorer `Session` and `Message` shapes, with
  no alias for the old ones.**
    - `POST /v1/chat/sessions` takes an optional `title` and `project_id`; `project_id` is
      fixed for the session's life. A project the tenant does not have is
      `422 reference_not_found`, and `profile_id` is `422 validation_error` until v2.1.0.
      The response is `201` with `session_id`, `title`, `project_id`, `profile_id`,
      `message_count`, `created_at` and `updated_at`; `greeting` is gone. An untitled
      session reads `title: ""`.
    - New sessions get `ses_…` IDs and new messages `msg_…` IDs. Stored `session-…`
      sessions keep their IDs and stay readable.
    - All three session routes take the tenant from the credential, or from `X-Tenant-Id`
      when it holds several; the `tenant_id` body field and `?tenant_id` query are
      removed.
    - `GET /v1/chat/sessions` answers `{"items": [...], "next_cursor": null}` instead of
      `{"sessions": [...]}`, adds `q` to search titles, and always sends `next_cursor`,
      `null` on the last page.
    - `GET /v1/chat/sessions/{session_id}/messages` answers
      `{"items": [...], "next_cursor": null}` **newest first**, instead of
      `{"messages": [...]}` oldest first. Page with `cursor` (was `after`) and `limit`
      1–100 (default 20, was 50 of at most 200). Each message carries `message_id`,
      `session_id`, `role`, `content`, `mode`, `citations`, `trace_id`, `feedback` and
      `created_at`; `token_count` and `partial` are gone. A user message reads
      `mode: null`, `citations: []` and `trace_id: null`. Each citation carries `n`,
      `chunk_id`, `document_id`, `title`, `snippet`, `url` and `page`; a citation stored
      before this release reads back numbered by position, with an empty `snippet`.
    - An answer cut short by a dropped stream is left out of history and out of
      `message_count`, and system messages are not listed. So are the empty assistant
      steps of 1.x agent turns.
    - Another reader's session, or an unknown one, is `404 not_found`.
- **`POST /v1/chat/completions` answers the explorer completion, with no alias for the
  old request or response.**
    - The body is `message` (1 to 8,000 characters, was `prompt`), optional `session_id`,
      `mode` (`classic` by default, or `instant`), `reasoning` (`off` only),
      `project_ids`, `connections`, `filters` and `stream`. `tenant_id`,
      `idempotency_key`, `model`, `graph_search` and `project_id` are removed and answer
      `422`; `mode: "rag"` is `422`, and `advanced` or `auto` is `422 mode_unavailable`
      until v2.1.0.
    - The tenant comes from the credential, or from `X-Tenant-Id`. The idempotency key is
      the `Idempotency-Key` header only, and `session_id` is now part of the request it
      guards.
    - Leaving out `session_id` asks a one-off question: `session_id: null` in the answer,
      nothing stored, and no session created. Create sessions with
      `POST /v1/chat/sessions`; their project is the turn's scope, and `project_ids` in a
      session is `422 validation_error`. A connection outside the scope is
      `422 connection_out_of_scope`.
    - Retrieval reads only the connections of the session's project (or of the one-off
      question's `project_ids`), at that project's `retrieval.top_k`. A project with
      nothing published is `409 no_indexed_content` before any model call.
    - The answer is `completion_id` (the stored assistant `message_id`), `session_id`,
      `trace_id`, `mode`, `answer`, `citations` (`n`, `chunk_id`, `document_id`, `title`,
      `snippet`, `url`, `page`) and `usage` (`mode`, `reasoning`, `steps`,
      `prompt_tokens`, `completion_tokens`, `reasoning_tokens`, `cost_usd`). `created`,
      `model`, `provider`, `provider_model`, `message`, `outcome`, `refusal_reason`,
      `finish_reason`, `cost`, `latency_ms`, `retry_count`, `fallback_count`, `title`,
      `project_id` and `memory_persisted` are gone.
    - `usage` adds up every model call of the turn, the `classic` scope check included,
      instead of only answer generation.
    - Answers cite sources as `[n]`, numbered from 1 in order of first use, instead of
      copying `[Source N: …]` labels. A bracket that does not name a retrieved passage,
      such as a year, an unknown `[9]` or text in code, stays as the model wrote it.
    - An out-of-scope request is an ordinary answer with no citations.
    - A provider failure, or an empty answer, is `503 llm_unavailable`, and a busy session
      is `409 session_busy` at once instead of after a 30 s wait; neither stores anything.
      The question is stored with its answer, not before the model call. An answer that
      could not be stored is not returned: the completion is `503 dependency_unavailable`
      instead of `200` with `memory_persisted: false`.
    - A `message` of whitespace only is `422 validation_error` on `message`.
    - An `Idempotency-Key` whose request failed before any model call (an outage, a
      refusal, a busy session) stays free for a retry; one that failed after is
      `409 invalid_state`.
    - With `stream: true`, the stream sends `response.started`, one
      `response.output_text.delta` with the whole answer, and `response.completed` with
      the JSON body, once the answer is complete. The `retrieval.completed`,
      `response.citations`, `response.warning` and `response.error` events are no longer
      sent.
- **An `Idempotency-Key` reused with another body is `422`**, not `409`; a key is kept
  for 24 hours. After that, the stored answer is deleted when the tenant's next keyed
  request arrives.
- **Chat completions are retrieval chat only:** `max_steps` is removed and answers
  `422`, the `response.agent.progress` stream event is gone, and
  `HARBORRAG_API_AGENT_TOKEN_BUDGET` and `HARBORRAG_MEMORY_AGENT_TOOLS` are ignored.
  Sessions created by the 1.x agent surface stay stored and are now listed by
  `GET /v1/chat/sessions` like any chat session. Session listings no longer carry
  `kind`, stored messages no longer carry `run_id`, and the `tool` message role is gone.
- **`harborrag chat`** calls `POST /v1/chat/completions` and prints the new answer;
  `-o json` prints the completion body. Without `--session-id` it asks a one-off
  question; see [Update scripts for CLI v2.0.0](#update-scripts-for-cli-v200).
- **SDK callers of the chat service** (`harborrag_app.workflow_control`, now
  `harborrag_runtime.application`; see
  [Python import paths and service methods](#python-import-paths-and-service-methods)):
    - `ChatExecutionOptions.session_id` is optional (`None` asks a one-off question),
      `project_id` is replaced by `project_ids` (one-off questions only), and `mode`,
      `connections`, `filters` and `trace_id` are new.
    - `chat_completion()` answers the explorer completion body and raises the turn's
      errors instead of returning a failed response for them; the new `chat_answer()`
      yields the turn's events (`RetrievalFound`, `AnswerDelta`, `TurnCompleted`).
    - `validate_chat_model`, `validate_chat_project` and `validate_completion_scope` are
      removed: the turn checks the model, the projects and the request's scope itself.

### Runtime settings

Six settings (`retrieval.top_k`, `retrieval.rerank`, `parsing.pdf_parser`,
`chunking.max_tokens`, `ingestion.concurrency`, `models.embedding`) can now be changed
through `/v1/admin/settings` ([Runtime settings](configuration/runtime-settings.md)).
Nothing changes at upgrade: a setting with no override keeps using the knob that governs
it today (`config/mcp/server.yaml`, `HARBORRAG_CHAT_RETRIEVAL_TOP_K`,
`config/ingestion/parsers.yaml`, `config/ingestion/chunking.yaml`,
`config/temporal.yaml`), and no document is reprocessed.

`GET /v1/admin/settings` shows the registry default for a setting with no override, even
when you changed its old knob. To carry a changed knob over, create
`config/settings.yaml` before the first start of the new API, with only the keys whose
old knob you changed and your value for each, for example `retrieval.top_k: 8`. Do not
copy `config/settings.example.yaml` whole: it lists every key you can set at its
registry default, and every key in the seed file becomes an override that replaces its
old knob, so a copied default would pin the default over a value you changed. The API
seeds the tenant `HARBORRAG_TENANT_ID` names from that file once, when the tenant has no
settings revisions. Set `HARBORRAG_SETTINGS_SEED_PATH` to read the file from another
path. After the seed, edits to the file only log a `config_yaml_drift` warning; change
settings with the API or `harborrag settings import`.

The first sync after any `parsing.*` or `chunking.*` override reprocesses that
connection, because both feed the processing identity.

`retrieval.rerank: true` is accepted when the model catalog has a `rerank` section, as
the shipped catalog now does; without one it is still refused with `422`. Nothing
reranks at upgrade, because the default stays `false`. Switch it on per project or for
the tenant to rerank chat retrieval; see
[Rerank chat retrieval](configuration/runtime-settings.md#rerank-chat-retrieval).

#### Use docling for PDFs

`parsing.pdf_parser: docling` needs two things HarborRAG does not ship:

1. A `parser: pdf` definition with a single `docling` engine in
   `config/ingestion/parsers.yaml`, added with `enabled: false` so the shipped
   `pdf-liteparse` definition stays the boot default. Copy the whole commented
   `pdf-docling:` block of `config/ingestion/parsers.yaml`, from the `pdf-docling:` line
   through its `engines` list and the last docling setting (`max_file_size`), into the
   active `parsers:` section uncommented, and change `enabled: true` to
   `enabled: false`. Keep its `engines` list to the single `docling` entry.
2. The `pdf-docling` extra of `harborrag-adapters` installed in the worker image, for
   example `pip install "harborrag-adapters[pdf-docling]"`.

Without them, PDF documents fail with `document_release_parser_unavailable`, recorded as
retryable. Add the definition, then run `retry-failures`; the retry replays the parser
the run was pinned to.

Adding the definition reprocesses every connection once, not only the one you switch to
docling. The parser identity is the digest of the whole `config/ingestion/parsers.yaml`,
so any edit to the file changes it for every connection, and the next sync of each
connection reprocesses all of its documents. To keep that to one reprocess:

1. Put the same `config/ingestion/parsers.yaml` in the API and in the worker before you
   set `parsing.pdf_parser: docling`. The API reads the file when it starts a run and
   the worker when it prepares a scheduled run, so with two different files the identity
   flips between the two kinds of run and documents reprocess on alternate syncs;
   docling documents also fail with `document_release_parser_unavailable` on a worker
   without the definition. The dev compose mounts the same `config/` into the API and
   the worker, so restart both after editing the file.
2. Then set the override.

A `retry-failures` run replays the processing identity of the run it retries. Documents
it publishes after you add the definition keep the old identity, so the next sync
reprocesses them once more.

### Known limitations

- **Vector and graph indexing still fail together.** A document is published only when
  both its vectors and its graph data are written. While the graph store is down,
  documents fail even when the vector store is healthy, and the cleanup of a failed
  document removes the vectors it had just written. Retry the failures once the graph
  store is back. Separate outcomes are planned for 2.1.
- `item_refs` on `POST /v1/admin/ingestions` answers `501 not_supported`.
- Project IDs are unique across tenants.

## 2.0.0a1 to 2.0.0a2

Alpha2 keeps each tenant's data under one storage namespace across Qdrant, FalkorDB and
the object store, and it changes how documents are projected into the graph. Stored data
is not migrated, so plan a reset and a full re-ingest.

### 1. Before you upgrade

1. Stop schedules and API submissions. Let every running HarborRAG workflow finish, or
   terminate them with the [Temporal CLI](https://docs.temporal.io/cli):

    ```bash
    temporal workflow terminate --query 'WorkflowType STARTS_WITH "harborrag"'
    ```

    An alpha1 run resumed by an alpha2 worker cannot find its plans and artifacts.

2. Back up the PostgreSQL control database. Restoring that backup, together with the
   untouched alpha1 stores, is the only practical rollback: alpha1 cannot start against
   a migrated database, and downgrading the schema loses data.

### 2. Update configuration

1. **Storage namespace.** Unset `HARBORRAG_QDRANT_COLLECTION_PREFIX` and
   `HARBORRAG_FALKORDB_TENANT_GRAPH_PREFIX`; they are ignored with a startup warning.
   Set `HARBORRAG_STORAGE_NAMESPACE_PREFIX` instead (default `harborrag`), and give each
   deployment its own value when they share stores. See
   [One tenant namespace in every store](configuration/workspace-mode.md#one-tenant-namespace-in-every-store).
2. **MCP limits.** Review `config/mcp/server.yaml`. Graph `max_depth` above 4 and
   `max_paths` above 5 are lowered with a warning.
3. **Confluence.** Remove `max_child_pages` from Confluence connectors; it is ignored
   with a warning.
4. **SharePoint.** Grant the app `Sites.Selected` on the configured sites only. A token
   with a broad role such as `Sites.Read.All` is refused at startup. Only `docx`, `pdf`,
   `pptx`, `xlsx`, `txt` and `md` files are ingested by default; set
   `allowed_extensions` to add others. For `auth_mode: device_code` on the Docker stack,
   rebuild the worker image, then run `scripts/deployment/dev.sh sharepoint-login`. See
   [SharePoint crawl scope, allowlists, and skips](configuration/connector-config.md#sharepoint-crawl-scope-allowlists-and-skips).

### 3. Clear the alpha1 data

Drop the alpha1 Qdrant collections, FalkorDB graphs and object-store keys, or keep them
until the upgrade is confirmed if you may need to roll back. Alpha2 uses these names:

| Store | Name |
| --- | --- |
| Qdrant collection | `{prefix}_{tenant}_{index}` |
| FalkorDB graph | `{prefix}_{tenant}` |
| Object-store keys | under `{prefix}_{tenant}/` |

### 4. Migrate the control database

Every HarborRAG process that opens the control database applies pending migrations at
startup. Start one process first (for example the API), wait until it is healthy, then
start the worker and the other services. The schema moves from revision 0019 to 0040; on
a large control database, migrations 0021 and 0037 hold table locks for a while.

### 5. Re-ingest

Re-ingest every source. The graph projection changed, so every document is reprocessed
without `--force-reprocess`.

Jira filter keys for custom fields with accented names have changed. Update saved
filters that use them.

### 6. Optional: keep entity summaries

Summarization is now off by default. To keep entity summaries on 2.0.0a2, set
`summarization.enabled` to `true` in `config/graph/build.yaml`, then run the 2.0.0a2
backfill command, `harborrag topology summaries backfill`.

That command exists only in 2.0.0a2. 2.0.0 removed it with the rest of
`harborrag topology`, so do not run it on 2.0.0 or later.
