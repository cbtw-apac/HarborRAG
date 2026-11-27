# Configure the 2.0.0 upgrade

Prepare your existing 2.0.0a2 deployment before you start any 2.0.0 process. Keep the current services on 2.0.0a2 until the rollout step in the upgrade guide.

Work down this checklist. After the applicable steps, return to the
[ordered upgrade guide](upgrading.md#200a2-to-200).

1. [Check database capacity, encryption and connector settings](#before-you-start).
2. [Move configuration files](#move-the-configuration-files) and update the
   [tenant variable](#rename-the-tenant-variable),
   [corpus mode](#set-the-corpus-mode-for-chat),
   [local source root](#set-the-local-source-root) and [model endpoints](#set-the-model-endpoint-variables).
3. [Check newly refused settings](#check-the-settings-that-are-now-refused) and
   [validate parser configuration](#parser-configuration).
4. [Prepare runtime settings overrides](#runtime-settings) if you plan to seed them on first start.

## Before you start

- Plan a maintenance window. The API, the MCP servers and the workers stay stopped while
  the control database migrates; see
  [Upgrade the services in order](upgrading.md#upgrade-the-services-in-order).
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
  now uses them too, where it used SQLAlchemy's default of 5 plus 10, so the API process
  can open up to 57 more connections. Workers, and inline runs from the CLI, open a new
  pool of the same size to read connections and their secrets, so each worker process
  can open up to 72 more. Size `max_connections` for every pool across the API process,
  the workers and any checkout CLI runs, or lower the two variables. See
  [Ingestion runtime environment](configuration/config-file-reference.md#ingestion-runtime-environment).
- Give the API the environment variables `config/sources/connectors.yaml` names, for
  example `JIRA_BASE_URL` or `LOCAL_SOURCE_PATH`. The one-time import reads non-secret
  values from the API's environment.
- With the Compose stack, let Docker pull two images. The data stack now pins Redis to
  `redis:7.4.11-alpine` and Postgres to `postgres:15.19-alpine`, the same major
  versions, and Postgres also mounts the script that creates the MCP server's read-only
  role. The next `scripts/deployment/dev.sh up` (or `dev.sh data`) pulls the images and
  recreates both containers. Their volumes are kept, so stored data stays, and the role
  script does not run on an existing volume.

## Move the configuration files

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

`config/settings.yaml` and `config/temporal.yaml` stay where they are.
`config/temporal.example.yaml` and `config/models.example.yaml` are
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

## Rename the tenant variable

**Breaking:** `HARBORRAG_INGESTION_TENANT_ID` is renamed `HARBORRAG_TENANT_ID`, and it
now sets the default tenant of the API as well as the workers and the CLI. There is no
alias: a process that still has the old name set stops at start-up with
`HARBORRAG_INGESTION_TENANT_ID was renamed to HARBORRAG_TENANT_ID`. Rename it wherever
it is set: in `env/.env.api` and `env/.env.temporal` (the workers' file), which the
earlier templates set to `DEFAULT`, in a shell that exports it, and in your deployment
manifests. The compose files pass `HARBORRAG_TENANT_ID` (`DEFAULT` unless set) to the API
and the workers. An API credential that holds every tenant (`*`) and sends no
`X-Tenant-Id` header now acts in that tenant instead of always `DEFAULT`.

## Set the corpus mode for chat

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

## Set the local source root

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

## Set the model endpoint variables

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

### Re-index when the embedding model changes

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

## Check the settings that are now refused

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
- **A model deployment with no `api_key` no longer borrows an environment key.** A chat,
  embedding or rerank deployment of `openai_compatible`, `litellm_proxy`, `vllm`,
  `infinity`, `ollama` or `custom` that leaves `api_key` out now sends the fixed
  placeholder `harborrag-no-api-key`. Before, LiteLLM read a key from the environment,
  such as `OPENAI_API_KEY`, `HOSTED_VLLM_API_KEY`, `INFINITY_API_KEY`, `OLLAMA_API_KEY`,
  `LITELLM_PROXY_API_KEY`, `COHERE_API_KEY` or `CO_API_KEY`, and sent it to the
  configured endpoint. A server that takes no key ignores the placeholder; one that
  needs a key now answers `401`. In a catalog of your own, give each such deployment
  the variable it relied on, for example `api_key: ${OPENAI_API_KEY}`. A `custom`
  deployment that reaches Amazon Bedrock must use the `bedrock` provider. LiteLLM's
  Ollama model lookup still sends `OLLAMA_API_KEY` to `OLLAMA_API_BASE`
  (`http://localhost:11434` unless set), so leave `OLLAMA_API_KEY` unset for the API,
  the workers and the MCP server. The shipped catalog sets every `api_key` and is not
  affected.
- **The MCP server needs its own read-only accounts.** The Compose MCP server and
  Explorer UI server no longer get the Postgres owner or the MinIO root credentials.
  They connect as the `harborrag_mcp` role and the `harborrag-mcp` MinIO user, with
  `POSTGRES_MCP_PASSWORD` and `MINIO_MCP_PASSWORD` from `env/.env.database`. Until that
  file holds both, every `mcp.sh` and `mcp-ui.sh` command stops, `down` included, with
  `run scripts/deployment/dev.sh mcp-access to set POSTGRES_MCP_PASSWORD`. Create the
  accounts as step 5 of
  [Upgrade the services in order](upgrading.md#upgrade-the-services-in-order) shows.
  `harborrag-mcp --local-stack-root` uses the same accounts, refuses to serve without
  both passwords, and no longer reads the `HARBORRAG_CONTROL_DB_URL` line of
  `env/.env.database`, which is the host CLI's owner URL.
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
  existing one. The same goes for the missing directories above
  `HARBORRAG_OBJECT_STORE_ROOT`, the reader-tool audit file and a SQLite database file:
  each is created with mode 0700 whatever the umask, and an existing one keeps its
  mode. A relative `HARBORRAG_OBJECT_STORE_ROOT` resolves against the process's working
  directory and logs one warning per process; set an absolute path.

## Parser configuration

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

## Runtime settings

Retrieval, capture, parsing, chunking, embedding, ingestion and model settings can now be
changed through `/v1/admin/settings`; [Runtime settings](configuration/runtime-settings.md)
lists every key. Nothing changes at upgrade: a setting with no override keeps using the
knob that governs it today (`config/mcp/server.yaml`, `HARBORRAG_CHAT_RETRIEVAL_TOP_K`,
each connection's own filters and size limit, `config/ingestion/parsers.yaml`,
`config/ingestion/chunking.yaml`, `config/temporal.yaml`), and no document is
reprocessed.

`GET /v1/admin/settings` shows the registry default for a setting with no override, even
when you changed its old knob. To carry a changed knob over, create
`config/settings.yaml` before the first start of the new API, with only the keys whose
old knob you changed and your value for each, for example `retrieval.top_k: 8`. Every
key in the seed file becomes an override that replaces its old knob, so a key written at
its default would pin the default over a value you changed. The API
seeds the tenant `HARBORRAG_TENANT_ID` names from that file once, when the tenant has no
settings revisions. Set `HARBORRAG_SETTINGS_SEED_PATH` to read the file from another
path. After the seed, edits to the file only log a `config_yaml_drift` warning; change
settings with the API or `harborrag settings import`.

The first sync after a `parsing.*` or `chunking.*` override that changes how documents
are parsed or chunked reprocesses that connection, because both feed the processing
identity.

`retrieval.rerank: true` is accepted when the model catalog has a `rerank` section, as
the shipped catalog now does; without one it is still refused with `422`. Nothing
reranks at upgrade, because the default stays `false`. Switch it on per project or for
the tenant to rerank chat retrieval; see
[Rerank chat retrieval](configuration/runtime-settings.md#rerank-chat-retrieval).

### Use docling for PDFs

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
