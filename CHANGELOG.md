# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

Documents are re-chunked and re-embedded on their next ingestion, so re-ingest every
source in `force` mode (`harborrag ingestions start PROJECT_ID/CONNECTION_ID --mode force`).
The `harborrag` commands are renamed to follow the HTTP API. The API migrates the control
database when it starts, so upgrade in a maintenance window with a single API replica.
Follow the [upgrade guide](docs/users/upgrading.md) before upgrading; entries marked
**Breaking** need action.

### Added

#### harborrag-adapters

- Migration `0046` stores runtime settings overrides and an append-only revision log
- Migration `0047` stores a chat session's `project_id` and an answer's `mode`, `trace_id` and `feedback`
- Migration `0048` drops the `DEFAULT` server default from `tenant_id` on nine control-plane tables, so the database no longer fills in a missing tenant; stored rows keep their tenant. It takes an `ACCESS EXCLUSIVE` lock on each table, with `lock_timeout` 5 s on Postgres; see the [upgrade guide](docs/users/upgrading.md#upgrade-the-services-in-order)
- **Breaking:** migration `0049` clears the raw secret values that migration `0041` copied from a legacy source row into a connection's `credentials`, `config` or `advanced` settings, and marks each such connection `error` with the cleared fields in its `status_detail`; set them again as the [upgrade guide](docs/users/upgrading.md#set-cleared-credentials-again) shows. Migrations have no encryption key, so the values cannot move into the secrets store. It waits at most 5 s for its row locks on Postgres, and Alembic's offline `--sql` mode leaves its data step out
- Structure-aware splitting of oversized text: code at syntax boundaries, Markdown at headings and fences, prose at sentence ends
- Markdown code blocks record their language, and source code files keep their indentation and are chunked as code

#### harborrag-app

- `GET` and `PATCH /v1/admin/settings` and `GET /v1/admin/settings/schema` read and change runtime settings
- CLI commands that call the same operation as their HTTP route, with the same input, output and error codes: `me`, `projects list|get|create|update|delete`, `source-types list`, `connections list|get|create|update|delete|test`, `ingestions start|list|get|pause|resume|cancel|retry-failures`, `failures list|retry` and `settings schema|get|set`
- CLI global options `--tenant`, `-o/--output table|json|yaml`, `--profile`, `--config` and `-y/--yes`, with defaults from `HARBORRAG_TENANT_ID`, `HARBORRAG_OUTPUT`, `HARBORRAG_PROFILE` and the profiles in `~/.config/harborrag/config.yaml` (keys `tenant` and `output`)
- `harborrag ingestions start --wait` and `ingestions get --watch` follow a run and exit `8` when it ends `failed` or `cancelled`; `ingestions start --inline` runs the pipeline in the CLI's process without Temporal
- The CLI asks before a write into a tenant that holds no data, `settings import --apply` included; `--yes` skips the question
- `harborrag doctor` lists the tenants that hold data and checks the services with the `/readyz` checks, Temporal included
- `make cli-reference` writes `docs/users/cli-reference/commands.md` from the CLI's help, the command registry and the OpenAPI export, with each command's HTTP route and the CLI-only and HTTP-only lists; a test fails when a command and its HTTP route drift apart or the page is stale
- `harborrag settings import` and `harborrag settings export` move runtime settings through a YAML file
- `harborrag --version` prints `harborrag VERSION` and exits `0` without reading a project, an environment file or the control database
- `harborrag doctor` validates `config/ingestion/chunking.yaml`
- `GET /v1/admin/failures` lists failed documents with `counts` per error category, and `POST /v1/admin/failures/retry` starts one retry run per original run
- HTTP chat and `harborrag chat` rerank their passages when `retrieval.rerank` is on for the session's project, or else the tenant; `PATCH /v1/admin/settings` and `harborrag settings import` accept `retrieval.rerank: true` when the model catalog has a `rerank` section, and refuse it with `422 validation_error` otherwise

#### harborrag-core

- `DocumentVersionPort.refresh_admission_change_key` stores the change key a source reports on an active version whose content has not changed
- Runtime settings registry: `retrieval.top_k`, `retrieval.rerank`, `parsing.pdf_parser`, `chunking.max_tokens`, `ingestion.concurrency` and `models.embedding`, with tenant, project and connection levels
- `RetrievalRequest.rerank` asks retrieval to rerank its passages with the model catalog's `rerank` model

#### harborrag-engine

- SharePoint chunks carry the document's `created_at`, `updated_at`, `author` and `web_url`, and its site, drive, item and page IDs, in their vector payloads, and a change to one of them makes a new document version; an existing document gets them when it is fetched again, so re-ingest SharePoint connections once in `force` mode, see the [upgrade guide](docs/users/upgrading.md#re-ingest-sharepoint-connections-once) [#121]

#### harborrag-mcp-server

- `vector_search` reads `retrieval.top_k` at tenant level as its default `top_k`; chat reads it at the level of the session's project
- `harborrag-mcp --preflight` connects every backend once and reports a start-up failure in one line

#### harborrag-runtime

- Cached settings provider that reloads on Postgres `LISTEN`/`NOTIFY` and a 5-second revision poll
- The API seeds settings from `config/settings.yaml` on first start and logs `config_yaml_drift` afterwards
- Ingestion pins the run's settings and `settings_revision` at start; workers parse and chunk with them
- Chunk sizes, per-source profiles and the splitting strategy are configurable in `config/ingestion/chunking.yaml`
- **Breaking:** ingestion requires the code grammars; the worker images and the `local` and `production` installs include them
- Retrieval can rerank with the model catalog's `rerank` model: it fetches four times `top_k` passages (at most 100), keeps the first `top_k` in rerank order, and adds `rerank_score` to each result's metadata while `relevance` keeps the lane's similarity; a rerank provider error fails the retrieval
- A warning when `HARBORRAG_SECRETS_ENCRYPTION_KEY` has spaces at its start or end; the key is still used as written, so stored secrets stay readable

#### Deployment

- `mcp.sh --http` checks the configuration before it starts the server and names model variables that no env file sets
- The MCP launchers say when the image is older than the source
- CI builds the MCP image and runs its handshake

### Changed

#### harborrag-adapters

- **Breaking:** `guarded_session` requires a `DestinationPolicy` and the GitHub, Jira, Confluence and SharePoint connectors take a `destination_policy` constructor argument instead of reading `HARBORRAG_CONNECTOR_ALLOWED_HOSTS` and `HARBORRAG_CONNECTOR_USE_PROXY` when they open a session; code that builds these connectors outside the runtime without a policy reaches public addresses only, see the [upgrade guide](docs/users/upgrading.md#connectors-built-outside-the-runtime)
- The SharePoint device-code sign-in (`harborrag_adapters.connectors.sharepoint.login`) sends its requests to Microsoft identity through the destination policy, built from `HARBORRAG_CONNECTOR_ALLOWED_HOSTS` and `HARBORRAG_CONNECTOR_USE_PROXY` by the command and public-only for `DeviceCodeLogin` built without a `destination_policy`, and refuses a tenant ID that is not a GUID or a domain name before it builds the endpoint URL
- GitHub and SharePoint connections read only inside their configured `root_path`, and a run's filters can only narrow the connection's: a source type registers how a run filter combines with the connection's, such as Jira ANDing a run `jql` onto its own, and a filter without one cannot be set by a run once the connection sets it; Jira's `order by` inside a quoted JQL value no longer ends the condition
- The local file connector sets a chunk's `created_at` to the file's creation time (`st_birthtime`, else `st_ctime`) and adds `updated_at` from the modification time
- The destination policy refuses a private IPv4 address carried in NAT64 (`64:ff9b::/96`), IPv4-compatible (`::/96`) or IPv4-translated (`::ffff:0:0:0/96`) IPv6 form, judging these addresses by the IPv4 address they carry; hosts and networks in `HARBORRAG_CONNECTOR_ALLOWED_HOSTS` keep working
- **Breaking:** the model catalog loader refuses a duplicate key, in YAML and JSON, and a top-level key other than `chat`, `embed` and `rerank`, so a misspelled section no longer turns a family off without a warning; `harborrag doctor` fails its `models catalog` check on such a catalog
- **Breaking:** a model deployment whose `api_key` is empty or only spaces is refused when its client is built, with an error that names the deployment, instead of LiteLLM sending an ambient key such as `OPENAI_API_KEY` to the configured endpoint; only a rerank deployment whose provider needs no key, `litellm_proxy`, `vllm` or `infinity`, may leave it empty. A chat catalog with `backend.type: litellm_proxy` needs a non-empty `backend.proxy.api_key`
- A model deployment's `api_key`, `model`, `api_base` and `deployment_name`, and the chat proxy's `api_key`, lose the spaces at their start and end, such as the CR of a CRLF env file, and a line break inside one is refused with an error that names the field
- Chat, embedding and rerank calls no longer retry inside the provider SDK under HarborRAG's own retries, and LiteLLM's module retry default is `0`
- Embedding and rerank caches are keyed by the model catalog, as chat is, so the first calls after the upgrade, and after any catalog change, miss the cache instead of returning the previous model's results
- **Breaking:** SharePoint discovery fails, and Temporal retries it from its checkpoint, when a folder listing or a selected item answers `429` or a `5xx`, or times out, instead of skipping the folder and retiring its files after the next full scans; only denied and deleted items are skipped. Local discovery skips a directory it may not read or that was removed while it ran, with the new skip reason `deleted`, and fails on any other listing error
- Requests that cannot succeed on retry are not retried: an HTTP `4xx` other than `408` and `429`, an oversized response, a redirect to an untrusted origin, a chained redirect and a URL outside the configured origin (the new `RefusedResponseError`), a GitHub `404`, recorded as `document_release_source_item_not_found`, a GitHub permission `403`, recorded as `document_release_authorization_failed` instead of a `StreamConsumedError`, and an embedding request the provider rejects with `400` or `401`. A GitHub secondary rate limit and a body that is not JSON are still retried
- The Qdrant, FalkorDB and S3 adapters raise HarborRAG storage errors: an outage, a timeout or a `5xx` answer is a transient error and is retried, a `429` is a rate limit, and a `400` or `422` is a validation error that is not retried. The API answers `503 dependency_unavailable` for a Qdrant gRPC or S3 outage, where it answered `500`

#### harborrag-app

- The Click requirement is raised to `>=8.4.0,<9` (it was `>=8.2.1,<9`), and a mistyped command prints its "Did you mean" hint once: Click 8.4 adds the hint itself, so the CLI no longer adds a second one
- `env-example/.env.api.example` sets `HARBORRAG_CORPUS_ACCESS_MODE=tenant_shared` and `HARBORRAG_CORPUS_SHARED_TENANT_ID=DEFAULT`, so chat finds sources on a new deployment; the code default stays `source_acl`, and removing the two lines returns the tenant to it. Nothing changes for an existing deployment until its `env/.env.api` gets the two lines and the API is restarted
- `env-example/.env.mcp.example` sets `HARBORRAG_CORPUS_ACCESS_MODE=tenant_shared` and `HARBORRAG_CORPUS_SHARED_TENANT_ID=DEFAULT`, so a reader key for `DEFAULT` reads the shared corpus like chat does. `HARBORRAG_MCP_READER_TENANT_ID` stays commented out, so the local bearer token keeps its grant on every tenant (`*`); set it to bind the token to one tenant, which `api_key` mode requires before it accepts the token. An existing `env/.env.mcp` stays on `source_acl` until it gets the two lines and the MCP server is restarted
- `harborrag auth keys revoke` takes `--key-id` alone, or `--owner` with an explicit `--tenant`, and never uses the default tenant or `HARBORRAG_TENANT_ID`
- **Breaking:** `harborrag auth keys create` requires an explicit `--tenant` and exits `2` without it, as `revoke --owner` does; it no longer falls back to `HARBORRAG_TENANT_ID`, the profile or `DEFAULT`. `list` keeps its default
- The CLI's last-resort default tenant is the `HARBORRAG_TENANT_ID` line of `env/.env.api` (or the file `API_ENV_FILE` names), in place of `DEFAULT`, so it acts in the stack's tenant; `--tenant`, an exported `HARBORRAG_TENANT_ID` and the profile's tenant still outrank it, and the line is not exported. `harborrag doctor`'s `corpus mode` row names the shared corpus's tenant when the CLI's tenant differs from it
- A script's run gets a trace of the project or checkout the CLI used: when stderr is not a terminal, `harborrag: using project DIR` and `harborrag: using repository checkout DIR` are logged at `INFO` (set `HARBORRAG_LOG_LEVEL=INFO`) instead of being dropped
- The CLI reads `HARBORRAG_CORPUS_ACCESS_MODE` and `HARBORRAG_CORPUS_SHARED_TENANT_ID` from `env/.env.api` (or the file `API_ENV_FILE` names) and nothing else in it, and an exported variable outranks the file; `harborrag doctor` shows the `corpus mode` for the acting tenant and warns on `source_acl`
- **Breaking:** `GET /v1/admin/ingestions/{ingestion_id}/events` sends `run.status`, `stage.progress`, `document.failed` and `run.completed`, replacing `task.<id>.progress`, `.done` and `.control`:
    - frames carry integer ids, a `: ping` arrives every 15 s, and `Last-Event-ID` resumes the stream
    - the response opens at once, and the deadline from `api_stream_timeout_seconds` ends the stream with an `error` event `deadline_exceeded`
    - a pause or resume appears as a `run.status` with the new status (`queued`, `running`, `paused`); the final status, including a cancel, arrives in `run.completed.finish_reason`
    - reconnecting to an ended run with nothing after `Last-Event-ID` answers `204`
- **Breaking:** the chat session routes answer the explorer `Session` and `Message` shapes:
    - `POST /v1/chat/sessions` takes `title` and `project_id` (an unknown project is `422 reference_not_found`, `profile_id` is `422` until v2.1.0) and answers the `Session`; `greeting` is gone
    - `GET /v1/chat/sessions` answers `items` of `Session` with `next_cursor` and searches titles with `q`
    - `GET /v1/chat/sessions/{session_id}/messages` answers `items` of `Message` newest first, pages with `cursor` and `limit` up to 100, and leaves out answers cut short by a dropped stream
    - the routes take the tenant from `X-Tenant-Id` instead of `tenant_id`
    - new sessions get `ses_…` IDs and new messages `msg_…`; stored `session-…` sessions keep theirs
- **Breaking:** `POST /v1/chat/completions` answers on the explorer contract, with no alias for the old request or response:
    - the body takes `message`, `mode` (`classic` or `instant`; `advanced` and `auto` are `422 mode_unavailable`), `reasoning` (`off`), `project_ids` (one-off questions only), `connections`, `filters` and `stream`; the tenant comes from `X-Tenant-Id` and the key from the `Idempotency-Key` header only
    - the answer is `completion_id`, `session_id`, `trace_id`, `mode`, `answer`, `citations` (`[n]` markers numbered by first use) and `usage` with `cost_usd`
    - `prompt`, `tenant_id`, `idempotency_key`, `model`, `graph_search`, `project_id`, `outcome`, `refusal_reason`, `provider`, `provider_model`, `message`, `cost`, `latency_ms`, `retry_count`, `fallback_count` and `memory_persisted` are removed
    - leaving out `session_id` asks a one-off question that creates no session and is not stored; in a session, retrieval reads only the session project's connections at its `retrieval.top_k`, and the answer is stored with the question under `completion_id`
    - an empty project is `409 no_indexed_content` before any model call, a busy session `409 session_busy` at once, and a provider failure `503 llm_unavailable` with nothing stored
    - `instant` skips the scope-check model call and graph observation, and an out-of-scope request is an ordinary answer without citations
    - `stream: true` sends the finished answer as `response.started`, one `response.output_text.delta` and `response.completed`
- **Breaking:** `harborrag chat` calls `POST /v1/chat/completions` and prints the new answer; without `--session-id` it asks a one-off question, and `--session` and `--project` are now `--session-id` and `--project-id`
- **Breaking:** the CLI follows the HTTP API, with no alias for the old names: `source list|show|add|update|test|remove` are `connections list|get|create|update|test|delete`, `source sync` and `ingest start` are `ingestions start`, `ingest run` is `ingestions start --inline`, `ingest status` is `ingestions get`, `ingest wait|watch` are `ingestions get --watch`, and `ingest pause|resume|cancel` are `ingestions pause|resume|cancel`; an old name prints the new one and exits `2`
- **Breaking:** `ingestions start` and the `connections` commands take the connection ref `PROJECT_ID/CONNECTION_ID`, refuse a bare key such as `harborrag-workspace` with a hint, and no longer import `config/sources/connectors.yaml`; `--force-reprocess` is `--mode force`, and `--timeout`, `--batch-size`, `--document-concurrency`, `--run-id`, `--connection-id`, `--source-scope-id`, `--path`, `--pattern`, `--recursive`, `--attachments`, `--filters-json`, `--limit`, `--events` and `--refresh` are removed
- **Breaking:** CLI `--json` is `-o json` and prints the HTTP response body instead of `{"ok", "data", "error"}`; errors go to stderr as the HTTP error envelope, or one line in a table; exit codes follow the HTTP status: `2` bad input, `3` not found (no longer a `--timeout`), `4` conflict, `5` not allowed, `6` unavailable, `8` a followed run that failed or was cancelled, or a connection test that reports `ok: false`, `130` interrupted
- **Breaking:** `harborrag projects update` replaces the project body like the `PUT`, so a field left out is cleared; `harborrag connections test` exits `8` when the check reports `ok: false`, and still prints the same body
- **Breaking:** `harborrag auth keys` prints the object itself instead of `{"ok": true, "data": …}`, takes `-o table|json|yaml` and the other global options, and reports a failure as the common error envelope with the CLI exit codes instead of `{"ok": false, "error_code": …}` and exit `1`. The secret still goes only to its `0600` file
- `harborrag --help` groups the commands under Setup and diagnostics (`me`, `doctor`), Access (`auth`), Chat and Resources, in place of the Operations panel
- **Breaking:** `harborrag config import` and `harborrag config export` are `harborrag settings import` and `harborrag settings export`, with no alias for the old group: `harborrag config` exits `2` as an unknown command
- **Breaking:** the export file option is `--file` (`harborrag settings export --file`; it was `config export --output`), and `harborrag doctor` drops `--json` and `--temporal`: it always checks Temporal and exits `6` when only a service check fails
- The CLI prints `harborrag: using repository checkout DIR` and `using project DIR` only when stderr is a terminal
- **Breaking:** chat completion failures and accounting change:
    - `503 dependency_unavailable` when the answer cannot be stored, `503 llm_unavailable` for an empty answer, and `422` for a whitespace-only `message`
    - `usage` adds up the scope check and the answer
    - an `Idempotency-Key` stays free after a failure before any model call, store and retrieval outages included
- **Breaking:** SDK chat: `ChatExecutionOptions.session_id` is optional, `project_id` became `project_ids`, and `mode`, `connections`, `filters` and `trace_id` are new; `chat_completion()` returns the explorer body and raises the turn's errors; `chat_answer()` yields the turn's events; `validate_chat_model`, `validate_chat_project` and `validate_completion_scope` are removed
- **Breaking:** every `/v1/admin/ingestions*` route answers the explorer `Ingestion` shape and the task shape is retired, reads need role `editor` and take the tenant from `X-Tenant-Id` (`?tenant_id` is removed), pause, resume and cancel answer `200` with the run, and `retry-failures` answers `202` with the new run
- Connection tests report `error_category` `secret_unavailable` when the API cannot read a secret, and say that an `env:` secret lives in the worker's environment
- `HARBORRAG_TEMPORAL_UI_URL` and `HARBORRAG_GRAFANA_URL` fill the `links` of an ingestion run
- `POST /v1/admin/ingestions` and `harborrag ingestions start` refuse a connection a run would reject, with `422 validation_error` naming each failing field, instead of starting a run that fails in the worker
- The CLI recognises a checkout by `config/sources/connectors.yaml`
- `POST /v1/admin/ingestions/{ingestion_id}/resume` and `harborrag ingestions resume` accept a `running` run whose workflow holds a pause it has not applied yet, such as a pause sent during discovery, and take the pause back; on a `running` run with no pending pause they answer `409 invalid_state`
- `harborrag ingestions start --inline` refuses a connection with a pending, running or paused run with `409 already_running` and exit `4`, as the API does
- An ingestion's `counters.discovered` and `counters.skipped` include the items skipped at discovery, so `processed + skipped + failed` reaches `discovered` when every item is done, and the CLI's run tables show a `Skipped` column [#136]

#### harborrag-core

- **Breaking:** `DocumentVersionPort.active_relation_document_ids` takes a required `tenant_id`, and code that implements the port must add `refresh_admission_change_key`

#### harborrag-engine

- A document whose only change is in its metadata gets a `METADATA_CHANGED` version that reuses its vectors and stores the new payload values. Every connector's change check now covers the provenance fields its chunks copy into vector payloads, such as a Jira issue's assignee, priority, status category, due date, resolved time, components and custom fields, and the document's creation and update times. Such a change was reported unchanged, payload filters kept the old values, and the item was fetched and parsed again on every run. The first run after the upgrade gives each item earlier releases re-fetched on every run a new version; the [upgrade guide](docs/users/upgrading.md#expect-a-larger-first-run) shows how to count them
- In `stat` checksum mode, a local file whose modification time changed gets a new document version that reuses its vectors, instead of being fetched again on every run
- A document that stops producing text, such as an emptied file or an attachment replaced by an image-only scan, has its active version retired and its vectors and graph data cleaned up; the run counts it as skipped with reason `no_indexable_content`
- `harborrag_engine.testing.retrieval_fakes` ships the in-memory retrieval fakes (embedding client, vector, chunk, graph and topology repositories, `resources()` and `policy()`) that the engine and runtime retrieval tests share; it imports only core and engine and no test framework
- Long code chunks keep the names of the class and function they belong to, and are searchable by them
- The `default` and `concise` chat prompts ask the model to cite sources by their `[n]` number

#### harborrag-mcp-server

- The MCP server reads `config/mcp/server.yaml` and, with the file key store, `config/mcp/keys.yaml`
- `harborrag-mcp` reads `HARBORRAG_MCP_AUDIT_PATH` once at launch and passes the path to the server; code that builds `McpServer(...)` or calls `create_mcp_server()` without a registry gets the default `.harborrag/mcp-audit.jsonl` and no longer reads the variable, so pass `audit=McpAuditLog(path=...)` to choose the path; see the [upgrade guide](docs/users/upgrading.md#mcp-server-built-in-python)
- **Breaking:** the MCP HTTP server refuses a request addressed to a host name other than loopback, or sent from another origin
- The MCP HTTP server answers extra host names, such as a reverse proxy's, when they are listed; a development container may listen beyond loopback when it acknowledges that
- The MCP status page walks through connecting, choosing a tenant that holds content, picking a tool and running it, in one window

#### harborrag-memory

- `delete_memory` refuses a GLOBAL memory with a `forbidden` error, which the HTTP layer answers as `403`

#### harborrag-runtime

- **Breaking:** `ConnectorPool` and `ConnectionService` require a `destination_policy` keyword argument, and `run_checks` takes a `destination_policy` parameter after `tenant_id`; `build_connector`, `build_connector_from_parts` and connectors built from a config file with `ConnectorDefinition.build` or `ConnectorCatalog.build` reach public addresses only unless the caller passes `destination_policy`, see the [upgrade guide](docs/users/upgrading.md#connectors-built-outside-the-runtime)
- **Breaking:** `config/` is organised by domain (`sources/`, `ingestion/`, `graph/`, `models/`, `mcp/`) and every default path follows; a deployment that sets each `HARBORRAG_*_CONFIG_PATH` keeps working
- **Breaking:** one `config/models/models.yaml` serves OpenAI and an OpenAI-compatible gateway, with a key per family, an optional Cohere embedding mode and an optional reranker. The chat provider variable and the shared gateway key `HARBORRAG_API_KEY` are no longer read; `HARBORRAG_EMBED_PROVIDER` is still read and selects `openai_compatible` (the default) or `cohere` for the embedding model
- **Breaking:** `HARBORRAG_INGESTION_TENANT_ID` is renamed `HARBORRAG_TENANT_ID` (setting `RuntimeSettings.tenant_id`) and also sets the API's default tenant, so the API, the workers and the CLI share one; the old name has no alias and stops startup
- **Breaking:** model variables with the `HARBOR_CHAT_`, `HARBOR_EMBED_` and `HARBOR_MEMORY_` prefixes, such as those the model smoke tests read, are renamed `HARBORRAG_CHAT_`, `HARBORRAG_EMBED_` and `HARBORRAG_MEMORY_`
- **Breaking:** `harborrag-runtime` depends on `harborrag-memory` directly and its `memory` extra is removed; `pip install harborrag-runtime[memory]` installs the same packages and prints a warning about the unknown extra
- **Breaking:** Python modules moved between packages with no alias at the old path: `harborrag_app.workflow_control` is `harborrag_runtime.application`, `harborrag_runtime.retrieval.*` is `harborrag_engine.retrieval.service.*` (only `facades` and `composition` stay), `RuntimeRetrievalService.connect` is removed in favour of `connect_retrieval_service`, the `harborrag_runtime.ingestion` release, normalizer, maintenance and source modules are in `harborrag_engine.ingestion`, and `harborrag_runtime.source_query`, `.tokenization` and `.ingestion.limits` are in `harborrag_core`; the usage, locks, presenters and directory modules of `harborrag_runtime.application.memory` are in `harborrag_memory` and `MemoryIdentity` is `harborrag_core.domain.identity.MemoryIdentity`, `event_prefix` is `harborrag_core.contracts.events.event_prefix`; the moved types are no longer re-exported from `harborrag_app`, `harborrag_runtime.ingestion`, `harborrag_runtime.retrieval`, `harborrag_runtime.temporal.schemas`, `harborrag_runtime.temporal.submission` or `harborrag_adapters.connectors`; see the [upgrade guide](docs/users/upgrading.md#python-import-paths-and-service-methods) for every path
- **Breaking:** `BaseAppService` and `AppService` lose `start_ingestion`, `ingestion_status`, `ingestion_result`, `control_ingestion` and `retrieve`; call `submit`, `get_task`, `pause`, `resume` and `cancel` on `AppService`, or `HarborRAG().retrieval.search`
- **Breaking:** loggers named `harborrag.app.workflow_control.*`, `harborrag.app.api.chat`, `harborrag.app.api.readiness`, `harborrag.runtime.retrieval*`, `harborrag.runtime.ingestion.cleanup`, `harborrag.runtime.ingestion.version_lifecycle`, `harborrag.runtime.ingestion.capture`, `harborrag.runtime.ingestion.document_pipeline`, `harborrag.runtime.ingestion.reindex`, `harborrag.runtime.tools.*` and the `settings_import` logger are renamed after the module that owns them (`harborrag.runtime.application.*`, `harborrag.engine.*`, `harborrag.memory.*`, `harborrag.app.operations.readiness`); log filters and alert rules that select them by name need the new names, see the [upgrade guide](docs/users/upgrading.md#logger-names)
- Config YAML files hold values only; their explanations moved to the configuration pages. `config/ingestion/parsers.yaml` keeps its comments because its bytes are the parser identity
- **Breaking:** `harborrag.yaml` and the SDK configuration file refuse a key under `runtime:` that is not a runtime setting, and the error names it, so a typo such as `contol_db_url` no longer falls back to the default database. This includes keys `harborrag.yaml` only exported as `HARBORRAG_*` variables, such as `local_source_root`, `log_level` and `connector_allowed_hosts`; set those as environment variables. `harborrag.yaml` matches keys in any case, the SDK file only in lower case
- **Breaking:** a connection's `advanced.max_retries` takes at most 10, and the source-type schema publishes that maximum; `HARBORRAG_MEMORY_RETENTION_DAYS_SESSION`, `_USER` and `_PROJECT` take at most 36,500 days
- **Breaking:** the API's control-plane engine sizes its pool with `HARBORRAG_CONTROL_DB_POOL_SIZE` and `HARBORRAG_CONTROL_DB_MAX_OVERFLOW` (40 and 32 by default), which apply to each control-database pool, instead of SQLAlchemy's default of 5 and 10; size Postgres `max_connections` for every pool of every API replica, worker and checkout CLI, see the [upgrade guide](docs/users/upgrading.md#before-you-start). The engine also checks a connection before use and recycles it after 30 minutes, so the first request after a database failover or an idle disconnect no longer fails
- **Breaking:** `AppService.resume` on a `running` run with no pending pause raises `IngestionActionStateError` (`invalid_state`) instead of `IngestionAlreadyRunningError` (`already_running`), and resumes a run whose workflow holds a pause it has not applied yet
- **Breaking:** a source or document run cancelled from Temporal, through its UI, its CLI or a cancelled schedule, is recorded as cancelled instead of failed, and a cancelled document's failure code is `document_release_cancelled` instead of `document_release_cancellederror`; update failure-inbox filters and dashboards that match it
- **Breaking:** document retries heartbeat, so a retry on a crashed worker is tried again after about 2 minutes instead of 60, and a cancel reaches it before it publishes. Workers that serve the `harborrag-io` queue must run this release no later than those that serve `harborrag-transform`, see the [upgrade guide](docs/users/upgrading.md#upgrade-the-services-in-order). The changed workflows sit behind seven new Temporal patch IDs, so runs started before the upgrade replay
- A scheduled firing is skipped while its connection has a pending, running or paused run, not only while its scan runs
- A request to a private address the operator has not allowed fails as `document_release_destination_blocked`, and a write that Qdrant or S3 rejects with `400` as `document_release_storage_request_rejected`; both are invalid configuration and are not retried in the run. A failure-inbox retry still includes `document_release_storage_request_rejected`, for after the configuration is fixed
- A run's `EXTRACTION_FAILED` skip count covers only parser rejections (`parser_rejected_document` and `document_unsupported`), no longer outages, a missing parser or cancellations at the parse stage

#### Deployment

- The development worker mounts `config/` read-only like the API and the MCP server, so a configuration change needs a restart, not an image rebuild
- `config/sources/schedules.yaml` and `config/mcp/keys.yaml` are git-ignored; each ships as an `.example.yaml` beside it
- `opentelemetry-api` requires `>=1.45.2,<2` in `harborrag-adapters` (the `opentelemetry` extra) and `harborrag-runtime`
- The `qdrant` extra of `harborrag-adapters` requires `qdrant-client>=1.17,<1.20` (was `>=1.10,<2`); the lock stays on 1.18.0
- `harborrag-app` requires `typer>=0.24,<0.25` (was `<0.27`); Typer 0.26 bundles its own Click, so the CLI's usage errors would print a traceback and exit `1`
- `config/temporal.example.yaml`, `config/models.example.yaml` and `config/harborrag.example.yaml` are removed
- `env-example/.env.models.example` sets a model name and a key for chat and for embedding, and keeps the direct-provider and other-proxy examples commented out
- **Breaking:** the MCP containers join the data network and publish their port on loopback; Docker Desktop host networking is no longer needed, and the data services must be started first, except for `mcp.sh --check`. The containers start only when `HARBORRAG_ENV=dev`, because the data services they reach use no TLS

### Removed

#### harborrag-adapters

- **Breaking:** GitHub, Confluence, Jira and SharePoint configs built in Python no longer read credentials from environment variables; pass them explicitly
- **Breaking:** the model catalog's `profiles:` section is removed, and a catalog that has one is refused; so are the `--profile` option of `python -m harborrag_adapters.models` and the `profile` argument of `prepare_config_section` and of the chat, embedding and rerank client configs' `from_file` and `from_dict`
- **Breaking:** storage code with no production caller: `harborrag_adapters.repositories.cache`, `harborrag_adapters.repositories.state` and `harborrag_adapters.repositories.database.{sqlalchemy,postgresql,sqlite,client,base}`. `harborrag_adapters.repositories.database` no longer exports `ChunkRepository`, `DocumentRepository`, `HarborDatabaseBackend`, `HarborUnitOfWork`, `HarborUnitOfWorkFactory`, `OutboxRepository`, `HarborDatabaseClient` or the `PostgreSQLDatabase*` and `SQLiteDatabase*` classes. Their tables were never in the migrations, so the database does not change
- **Breaking:** `harborrag_adapters.models.runtime.litellm_telemetry` and `LiteLLMTelemetryCallback`, which no client registered
- **Breaking:** the `HarborParserEngineRegistry` alias of `HarborParserRegistry`, and the `harborrag_adapters.parsers.pdf.engines.<engine>.mapping` modules, which re-exported helpers of `harborrag_adapters.parsers.pdf.normalization`

#### harborrag-app

- **Breaking:** `harborrag init` and its scaffold templates are removed with no replacement; run the CLI from a repository checkout. Projects an earlier `init` created keep working through their `harborrag.yaml` or `--project DIR`
- **Breaking:** `harborrag retrieve` and every `harborrag topology` command, `topology summaries backfill`, `topology config validate` and `apply` and `topology indexing permissions-import` included, are removed; search with the MCP `vector_search` tool or `harborrag chat`
- **Breaking:** `authorize_tenant` and `authorize_task_tenant` of `harborrag_app.api.auth.dependencies`, `Principal.tenant_scope` of `harborrag_app.operations.principal`, and `IngestionStatus` of `harborrag_app.operations.schemas.ingestions`, which had no caller

#### harborrag-core

- **Breaking:** `HarborEmbedCapabilities.max_input_tokens`, which nothing read; a catalog that sets it is refused

#### harborrag-engine

- **Breaking:** `harborrag_engine.retrieval.service.knowledge` and the `RuntimeRetrievalService` methods `fetch_evidence`, `resolve_entities`, `lookup_entities`, `find_semantic_relations` and `find_semantic_paths`

#### harborrag-runtime

- **Breaking:** the SDK's `HarborRAG().knowledge.fetch_evidence`, `resolve_entities`, `lookup_entities`, `find_relations` and `find_paths`, left from the 1.x agent; `find_paths` had no deadline. `HarborRAG().knowledge` keeps `list_documents`, `read_evidence`, `get_document_context` and `list_sources`
- **Breaking:** `AppService.chat_stream` and `BaseAppService.chat_stream`, an SDK-only stream that skipped the session's project scope, with `harborrag_runtime.application.chat.{turn,stream_result,events}`, the chat presenters `evidence_data`, `cited_evidence`, `chat_response_data`, `chat_stream_chunk_data` and `source_heading`, `prompting.EVIDENCE_PREAMBLE`, and `MEMORY_WARNING` and `STREAM_ERROR` of `chat.service`. `chat_completion()`, `chat_answer()` and `HarborRAG().chat.stream` stay
- **Breaking:** the memory extraction queue, which nothing fed: `harborrag_runtime.application.memory.extraction` (`MemoryExtractionQueue`, `submit_exchange`, `drain_extraction_queue`, `dropped_extractions`) and `AppService.start_memory_extraction` and `drain_memory_extraction`. `HARBORRAG_MEMORY_EXTRACTION_MIN_IMPORTANCE` and `HARBORRAG_MEMORY_DEDUP_THRESHOLD` are ignored, and refused under `runtime:`; `HarborRAG().memory.extract` and `HARBORRAG_MEMORY_EXTRACTION_ENABLED` stay
- **Breaking:** the `harborrag_runtime.ingestion.connector_metrics`, `discovery_metrics` and `observability_types` modules; import from `harborrag_runtime.observability.<module>`
- **Breaking:** `ConnectionImportService.export` and `EXPORT_VERSION` of `harborrag_runtime.config.connectors.importing`, which had no caller since the `source export` command was removed

#### Development

- `make provider-matrix` and `scripts/generate_provider_matrix.py`, which scanned folders that no longer exist, and the empty `scripts/models/download_docling_models.py`, `download_fastembed_models.py` and `warmup_models.py`

### Fixed

#### harborrag-adapters

- Chat history and `message_count` leave out the empty or whitespace-only assistant steps of 1.x agent turns, and migration `0047`'s downgrade waits at most 5 s for its locks on Postgres, like its upgrade
- **Breaking:** LiteParse and image OCR settings outside their permitted values are rejected, instead of being replaced by a default or failing on every document
- SharePoint connector and sign-in errors no longer show the client secret, refresh token, device code or access token, and failed token requests report their HTTP status [#123]
- Local, GitHub and SharePoint files whose name ends in a space keep their file type instead of being skipped
- Attachments whose name ends in a space are no longer skipped, and no longer get past an excluded file type
- Local Markdown links in angle brackets, or to a name that starts with a space, now link the two documents
- A local folder whose name ends in a space can be excluded
- Attachment failure logs name the attachment by its ID instead of its title
- SharePoint path allowlists match folders named with a no-break space, and accept a folder path that starts with the library's name, also for a library chosen by its ID
- Files whose name ends in a zero-width space keep their file type instead of being skipped
- Confluence Data Center page links keep a `+` in the page title
- A Confluence page whose own or parent page's title is only no-break spaces is ingested instead of failing
- Connector downloads follow a redirect whose target has accented or other non-ASCII characters, in any encoding, instead of failing
- A malformed link in a local Markdown file no longer fails the document
- The Confluence connector drops a body's attachment references that the attachment list supersedes, matching body media nodes to listed attachments by file ID
- A SharePoint `tenant_id` that is not a GUID or a domain name is refused: `connections create`, `update` and `test` answer a field error on `credentials.tenant_id`, and a connector config built in Python raises `ValueError`, instead of failing when the first token is requested
- A very large document no longer exceeds Qdrant's 32 MiB request limit: upserts go in requests of at most 128 points
- Jira Data Center search, comment and changelog paging read every page when the server lowers `maxResults`
- A local file swapped for a FIFO after discovery is refused instead of hanging the worker
- A Jira custom field whose normalized name is longer than 128 characters, or an issue with more than 128 typed fields, no longer fails the issue: such a field is filterable as `fields.customfield_<id>`, and the fields past the first 128 are left out with a warning
- A model error that was already a HarborRAG error is no longer raised from itself, so Temporal records its failure code instead of failing to encode it
- One connection row that fails validation, such as a row that still holds a raw secret, no longer fails the connection listing of the whole install: the listing skips the row and logs its ID
- A document version that fails while it is being verified is no longer marked verified
- SQLite control databases enforce foreign keys, so deleting a connection on a development database removes its document versions; rows an earlier release left behind stay
- The MCP tenant directory logs a warning when it stops at its limit of 500 tenants

#### harborrag-app

- `harborrag doctor` walks up from the current directory to the repository checkout, like the other commands, and no longer warns that `harborrag.yaml` is missing inside one; it reads `HARBORRAG_LOCAL_SOURCE_ROOT` instead of `LOCAL_SOURCE_PATH` and warns when the root is unset, because the API then refuses every `local_file` connection, and blank credentials of enabled remote connectors are a warning that names the connectors, so a fresh checkout exits `0`
- **Breaking:** `GET /v1/admin/settings/schema` answers `{"groups": [...]}` with a description per setting, `POST /v1/admin/failures/retry` answers `409 invalid_state` when no failure matches instead of `202` with a null `ingestion_id`, and `GET /v1/admin/failures` omits `total` unless `include_total=true`
- A connection's `stats.errors` counts the failed documents of its latest run, also after a cancel, and is `0` before any run
- A run whose Temporal workflow ended without recording it, such as one terminated in the Temporal UI, no longer blocks its connection with `409 already_running`, retry runs included: starting an ingestion, removing the connection and cancelling the run record it as `CANCELLED` or `FAILED` first
- Cancelling a retry run cancels its workflow and records the run as `cancelled`, instead of answering `503`; `pause` and `resume` on a retry run answer `409 invalid_state`
- Cancelling a running or paused run whose Temporal workflow is gone, such as one past the namespace retention, records it as `cancelled` with `error_category` `workflow_not_found`, over HTTP and with `harborrag ingestions cancel`, instead of answering `503` every time; a queued run that is not a retry run still answers `503`
- `POST /v1/admin/ingestions` with an `updated_after` that has no UTC offset answers `422 validation_error` on `updated_after`, instead of `500`
- `harborrag ingestions pause`, `resume` and `cancel` follow the HTTP rules: pausing a paused run, pausing or resuming a retry run, and any action on a finished run exit `4` with `invalid_state` and send no signal, cancelling a retry run cancels its own workflow instead of answering "task not found", and an unknown run is not found
- A relative file path typed in a project subdirectory, for `--file` of `projects`, `connections` and `settings`, `settings import FILE` and `--config`, names that subdirectory's file instead of one at the project root
- `--config -`, `settings export --file -`, `settings import -` and `auth keys create --secret-output -` are refused with exit `2`, instead of using the default profile or writing a file named `-`
- `harborrag auth keys` prints the warning that the control database is the local SQLite default also when the command fails, so a `not_ready` error carries the hint
- `harborrag settings export --file` no longer refuses to run in a repository checkout, because it only reads the control database
- `harborrag ingestions start --inline` accepts a `local_file` connection whose `source_path` is one file, as the connector does
- Ingestion document and failure lists keep the spaces at the start or end of a document's reference
- `PUT /v1/admin/projects/{project_id}` with a body that changes nothing keeps the project's version and `ETag` and records no activity entry, so repeating it with `If-Match` no longer answers `412` [#125]
- The CLI refuses a malformed connection ID in `connections get`, `create`, `update`, `delete` and `test` with `422 validation_error` and exit `2`, as HTTP does, instead of `500` or a not-found error; `connections list --source-type` takes only `jira`, `confluence`, `sharepoint`, `local_file` and `github`, and refuses anything else the same way instead of printing an empty page
- `harborrag me` reports the tenant the other commands use, including the tenant in `env/.env.api`
- The failure inbox routes release their API capacity when the handler returns, as the other routes do

#### harborrag-core

- Local and GitHub files with spaces in their file or folder name are ingested instead of failing
- Files whose paths differ only in spacing, such as a trailing or no-break space, stay separate documents
- A connection that ingested files whose paths hold a double space, a tab or a no-break space, or start or end with a space, on 2.0.0a1 or 2.0.0a2 no longer fails every run: its first scan moves those source items to the document ID this release gives them

#### harborrag-engine

- A retry after a change to chunk metadata, such as the local file timestamps, adopts the chunk set an earlier attempt stored, never replacing it, and rebuilds its index from the stored bytes
- SharePoint folders with spaces or accents in their name appear in the graph under that name instead of its URL encoding
- Local files whose relative paths differ only in spaces at their start or end stay separate documents and graph nodes, and reindex no longer fails on such a path
- A document fetched again and found unchanged stores the change key its source reports, so the next run skips it instead of fetching and parsing it on every run

#### harborrag-mcp-server

- Saving from the status page answers a clear error when the configuration file is read-only, and the page disables Save
- The status page's tool runner answers a failure inside a tool with the error type only, as the MCP endpoint does, and logs where it was raised without the error's message
- A server that listens on every interface no longer prints an address that cannot be opened

#### harborrag-runtime

- A `parsers.yaml` definition for one format, such as `docx` or `csv`, replaces only that format's engine, so the other formats of its family, such as DOC, ODT, EPUB or Excel, still parse
- **Breaking:** the API refuses to start on an invalid `parsers.yaml`, as the worker does, and warns when the file is missing
- **Breaking:** a wrong `parsers.yaml` setting value, or two enabled definitions for one parser family, is rejected when the file loads instead of passing the API and failing only in the worker
- Starting an ingestion while `parsers.yaml` is missing reports the missing file
- Starting against a control database a newer build already migrated names both schema revisions and how to recover, instead of only "migrations failed"
- The first-start import gives a Jira or Confluence Cloud entry without its email the `api_token` method, instead of the Data Center `pat` method, so a `PUT` of the email fixes it
- Cancelling a run keeps its summary, so the run's `discovered` count no longer drops when it is cancelled
- Reading a run Temporal reports as natively paused shows `paused` instead of failing with `8 is not a valid WorkflowExecutionStatus`
- Pausing a run no longer strands it: a run and its batch are paused by signal only, not natively in Temporal, where a workflow task scheduled at the pause was never dispatched again and resume and cancel hung; the Temporal UI now shows a paused run as `Running`
- A chat completion the response model rejects no longer marks its `Idempotency-Key` as completed; a retry answers that the first request failed, instead of that its result predates this API version
- A blank `HARBORRAG_INGESTION_TENANT_ID` counts as unset when the settings are read
- Folder and glob settings of local, GitHub and SharePoint connections keep spaces at their start or end. A stored value already lost them, so set such a value again with its spaces, as the [upgrade guide](docs/users/upgrading.md#put-back-spaces-in-folder-and-glob-settings) shows
- **Breaking:** SharePoint connections refuse a page size above 100, as Jira and Confluence do [#135]
- A brief error during finalization no longer marks a run `failed` while Temporal retries it; a run that runs out of retries fails at the stage finalization recorded, with an `error_code`
- A run, a retry run included, no longer sends every `document.failed` event again when it ends, and a run summary written from an earlier read no longer rolls the event cursor back
- A pause or cancel that arrives during discovery is always recorded, and a graceful cancel that races the end of a batch no longer fails the run
- A cancelled run's result reports the items skipped at discovery and per document, not only those skipped at discovery
- A document retry cancelled in Temporal is recorded as cancelled and its workflow closes as cancelled; the retry run counts that document as failed and finishes
- Relation repair after a reindex selects only the reindexed tenant's documents, so another tenant's documents no longer crowd them out of its row limit
- A connection removal whose store connections partly fail closes the stores that did connect before it reports the error
- Citation renumbering rewrites a marker only when every number in it names a passage, and leaves other brackets as written, such as years, an unknown `[9]` or text in code; an answer that is only an unknown marker is returned instead of failing
- A completion body the API would reject is no longer stored in the session's history, so the next turn does not replay it

#### Deployment

- The development API receives `JIRA_EMAIL`, `CONFLUENCE_EMAIL`, `MICROSOFT_TENANT_ID`, `MICROSOFT_CLIENT_ID` and `GITHUB_REPOSITORY_URL` from `env/.env.connector`, so the first-start import no longer stores the Jira, Confluence and SharePoint connections without them and with status `error`
- The SharePoint entry of `config/sources/connectors.yaml` crawls the drive with the shared file type list instead of a test folder of Markdown files
- `mcp.sh --http` stops and removes a server that never becomes healthy
- The development worker receives `HARBORRAG_INGESTION_TENANT_ID` from the compose environment file, so it refuses to start on the removed name as the API does

### Security

#### harborrag-adapters

- Header values whose name contains `key`, `token`, `auth`, `secret`, `signature` or `cookie` are masked when a model config is printed or logged, where a list of four names was masked before. A model request's `custom_headers` with such a name, such as `Idempotency-Key` or `X-CSRF-Token`, is refused unless the family's `security.allow_request_auth_headers` is on, and the chat proxy refuses such request headers by the same pattern
- Model deployment and chat proxy configs keep their input out of validation errors
- LiteLLM reads its packaged cost map instead of fetching it from GitHub when it is imported; set `LITELLM_LOCAL_MODEL_COST_MAP=false` to fetch it. It no longer prints a debug banner to stdout on a provider error, and its exceptions leave out the request messages
- Parser log lines and parser errors name a file by the first 12 hex characters of the SHA-256 of its name, with its suffix and size, instead of its name or path; local and GitHub connector logs and local load errors name a path by its digest the same way. A PDF or OCR engine failure that a library raised is named by its exception class instead of the library's message
- SharePoint and Atlassian request failures no longer carry the underlying request error, whose text can hold a signed download URL
- The destination policy refuses the `fec0::/10`, `3fff::/20` and `5f00::/16` IPv6 ranges, which are not public

#### harborrag-app

- **Breaking:** a blank `HARBORRAG_METRICS_SCRAPE_TOKEN` counts as unset, so `/metrics` on the ops port answers `401` to every request, where a bare `Bearer` header read it before; set a non-empty token. `env-example/.env.api.example` ships the line blank and `scripts/deployment/dev.sh` fills it in
- **Breaking:** over HTTP, an API key reads documents as its owner, as it does over MCP: a grant to the owner reaches the key's chat answers, and an explicit deny for the owner hides documents from them. `/v1/me`, rate and capacity limits, `Idempotency-Key` replay and session ownership still use the key ID
- Under `source_acl`, `GET /v1/projects/{project_id}/connections` lists only connections with a source the caller may read, so a connection never ingested is hidden; a caller who may read more sources than the permission check can list gets `409`. `tenant_shared` lists every connection as before. Each connection's `documents` count is still its total, not what the caller may read
- The HMAC token verifier keeps its signing secret out of its `repr`

#### harborrag-engine

- API key verification remembers a key ID with no stored key for 5 s, so repeating a made-up key no longer costs a database query each time; a wrong secret, a revoked or expired key and a store outage are still checked on every call
- Under `source_acl`, `list_sources` counts documents and reports the last ingestion time from the documents the caller may read, not the whole source, and `resolve_graph_nodes` shows nodes the tenant owns to any reader of the tenant
- Reader tools return an error's text only when the tool's arguments were wrong, so a backend error that quotes stored content stays opaque

#### harborrag-mcp-server

- Each credential gets at most `HARBORRAG_MCP_RATE_LIMIT` requests per second (default 10) and `HARBORRAG_MCP_MAX_INFLIGHT` requests at once (default 4), shared by `/mcp` and `/api/tools`; `0` turns a limit off, and a value that is not `0` or a positive whole number stops the launcher. A refused `/mcp` request gets the JSON-RPC error `-32000`, and `/api/tools` answers `429` with `Retry-After`. Each server process keeps its own budget, and the status page, `/healthz` and stdio are not limited

#### harborrag-memory

- Erasing a user deletes every chat session of the user, with its messages, title and session memories, and every completion request the user stored, with or without a session; before, it found sessions only through the memories they produced, which 2.0 chat does not write. `ErasureReport.as_dict()` adds `completion_requests`
- A stored completion body is deleted once its 24-hour key retention has passed, when the tenant's next keyed request arrives, instead of being kept

#### harborrag-runtime

- **Breaking:** `HARBORRAG_QDRANT_ALLOW_INSECURE_REMOTE`, `HARBORRAG_FALKORDB_ALLOW_INSECURE_REMOTE`, `HARBORRAG_TEMPORAL_ALLOW_INSECURE_REMOTE` and `connection.allow_insecure_remote` in `config/temporal.yaml` apply only when `HARBORRAG_ENV=dev`, as the Redis and object-store switches already did; in `staging` and `prod`, remote Qdrant, FalkorDB and Temporal need TLS
- A failed settings check of the API, a worker or the MCP server no longer prints environment values, such as object-store secrets or `HARBORRAG_AUTH_SECRET`
- An ingestion activity failure no longer carries the original error into Temporal history; it keeps the failure's safe code

### Known limitations

- Vector and graph indexing still fail together: while the graph store is down, documents fail even when the vector store is healthy, and the cleanup of a failed document removes the vectors it had just written. Separate outcomes are planned for 2.1

## [2.0.0a2] - 2026-10-26

Stored data is not migrated from 2.0.0a1: back up the control database and re-ingest every
source. Follow the [upgrade guide](docs/users/upgrading.md) before upgrading; entries marked
**Breaking** need action.

### Added

#### harborrag

- The `harborrag` CLI ships with every install; `harborrag[local]` adds the local-stack clients [#59]

#### harborrag-adapters

- Parsing for legacy Word `.doc` and PowerPoint `.ppt`/`.pps` files [#85]
- SharePoint connector: crawl sites, libraries, folders or selected files, with allowlists and optional site pages [#90]
- SharePoint device-code sign-in, including for the Docker worker [#90] [#99]
- Jira custom fields are filterable on issues and their attachments [#85]

#### harborrag-app

- `harborrag init` scaffolds a ready-to-run project with provider presets and data-source setup [#59]
- `harborrag ingest run` runs an ingestion in-process, without Temporal [#59]
- `harborrag doctor` checks the project, configuration and every backing store [#59] [#95]
- `harborrag auth keys` issues, lists and revokes tenant-bound MCP reader keys [#85] [#89]
- `--timeout` option for `ingest watch`, `wait` and `start --wait` [#95]
- `ingest start` takes the connector positionally, like `ingest run` [#95]
- Pause, resume and cancel actions are recorded in the run's events [#85]

#### harborrag-engine

- Placeholder graph nodes for links to sources not yet ingested, replaced once the target is published [#85]

#### harborrag-mcp-server

- `find_entities` tool to search whole entities by facet [#85]

#### harborrag-runtime

- Automatic purge of retired document versions after a configurable retention period [#85]
- Searchable entity summaries that lead to their supporting evidence [#85]
- Summary facets: filterable attributes copied from connector fields [#85]
- Configurable entity summary length [#85]

### Fixed

#### harborrag-adapters

- Corrupt or encrypted `.xls` files are reported as parser rejections [#85]
- SharePoint handles deleted items and expired tokens without failing the run [#96]
- Legacy secrets are migrated to the correct tenant [#96]

#### harborrag-app

- Interrupting or failing `ingest run` no longer leaves the run stuck [#95]
- The API no longer submits the same run twice [#95]
- `ingest pause`, `resume`, `cancel`, `status` and `watch` report correct results for finished and partial runs [#95]
- Clearer error messages that never include credentials, and faster CLI startup [#95]

#### harborrag-engine

- Retrieval works for users who can read more than 10,000 documents [#96]
- Entity search honours all filters, and facet filters are case-insensitive [#96]
- Graph reads and relation repair handle placeholder nodes and cross-scope links reliably [#96]

#### harborrag-mcp-server

- Tool output schemas are valid for MCP clients again [#85]
- `find_entities` reports unknown facets instead of returning an empty result [#96]
- A broken API key file denies access instead of revoking every key [#96]
- Over-limit `mcp.yaml` values are lowered with a warning instead of stopping the server [#96]

#### harborrag-memory

- Memory retention settings are enforced [#96]
- Memory recall works without an embedding model [#96]

#### harborrag-runtime

- Large sources no longer time out during discovery [#85]
- Pausing and resuming an ingestion no longer fails the run or loses its progress [#96]
- Re-ingesting content while its old version is being purged retries instead of failing [#96]
- A retried chat request no longer interferes with a slow response still in progress [#96]
- Summary and chat client failures no longer take down ingestion workers [#96]
- A late cancellation can no longer overwrite a completed agent run [#96]
- A warning is logged when a removed storage prefix setting is still set [#96]
- One concise log line per failed document [#95]

#### Deployment

- Removed unused variables from `env-example` [#96]

### Changed

#### harborrag-adapters

- **Breaking:** one storage namespace per tenant across Qdrant, FalkorDB and the object store; re-ingest after upgrading [#85] [#96]
- LiteParse is the default parser, and the OCR server is now opt-in [#85]
- `config/parsers.yaml` supports environment variables [#85]
- **Breaking:** SharePoint requires the `Sites.Selected` permission and limits file types by default [#90]
- SharePoint shares one rate limit across workers, skips unreadable folders, and caps retries per file [#96] [#101]
- **Breaking:** Jira custom-field filter keys handle accented names; update saved filters and re-ingest [#96]
- Local connectors reject unknown filters, and partial scans no longer retire documents [#95]

#### harborrag-app

- **Breaking:** standardized CLI exit codes; failed or partial runs now exit 1 [#95]
- In a repository checkout, ingestion uses the stack's PostgreSQL and object-store settings instead of a local SQLite database [#95] [#99]
- `auth keys` requires an up-to-date control database and reports clearer errors [#95]
- Run ids cannot be reused [#95]
- Clearer ingestion API responses when Temporal is unreachable or no longer knows a run [#95]
- Inline progress display replaces the full-screen dashboard [#59]
- Local commands no longer require Temporal to be installed [#59]
- `harborrag init` keeps existing project files [#96]
- CLI file paths resolve from the current directory [#96]

#### harborrag-engine

- Each source link is stored as a single graph edge, and duplicate attachment edges are removed [#85]
- Duplicate search results are collapsed per source instead of across the whole corpus [#85]
- Vector search results link directly to their graph nodes [#85]
- More accurate unresolved-relation counts [#85]

#### harborrag-mcp-server

- **Breaking:** graph chunk edges now point from structure to chunk, and graph traversal limits are lower [#96]
- The MCP Explorer respects each tenant's limits [#96]

#### harborrag-memory

- Project-scope memories stay within the session unless enabled, and summary prompts are bounded [#96]

#### harborrag-runtime

- Summarization is off by default [#85]
- Summaries indicate when some source documents are still missing [#85]
- Summary reads no longer lock or write data [#85]
- Faster summary updates and publication [#96]
- Retired-version cleanup starts sooner and exposes metrics [#96]
- Ingestion defaults tuned for smaller hosts [#85]
- Faster ingestion workflows, with per-document cost independent of source size [#85]
- The secrets encryption key is only required by processes that open secrets [#85]
- Failed documents report a clear error code and stage, and parser rejections are not retried [#95]
- Startup fails on mismatched embedding dimensions [#95]

#### Deployment

- MinIO uses a new image that runs as a non-root user [#85]

### Deprecated

#### harborrag-adapters

- The Confluence `max_child_pages` setting is ignored with a warning [#96]

### Removed

#### harborrag-app

- Textual ingestion dashboard [#59]

#### harborrag-mcp-server

- **Breaking:** `composed_evidence_search`, `verify_citations` and `get_document_metadata` tools; use `vector_search`, `fetch_evidence` and `get_document_context` instead [#85]

#### harborrag-memory

- Unused `HARBORRAG_MEMORY_PII_REDACTION` setting [#96]

### Security

#### harborrag-adapters

- The `.ppt` parser is protected against crafted files that expand into huge output [#96]
- SharePoint tokens are stored safely, and pre-authenticated download links are no longer kept [#96] [#98]
- Model configuration errors no longer print API keys [#98]

#### harborrag-app

- Project discovery only trusts directories owned by the current user [#59]
- Errors and logs no longer expose credentials [#95]
- Unauthenticated mode refuses non-local requests unless explicitly allowed [#98]

#### harborrag-core

- More credential field names are detected and redacted [#98]

#### harborrag-engine

- Retrieval diagnostics no longer reveal documents the caller cannot read [#96]

#### harborrag-mcp-server

- Reader keys cannot target all tenants [#85]
- In API key mode, the owner token is only accepted for single-tenant deployments [#96]
- The MCP server no longer receives the secrets encryption key [#96]

#### harborrag-memory

- Shared memories can only be deleted or replaced by their author or a user with the required role [#96]

#### harborrag-runtime

- A tenant reindex no longer touches other tenants' cleanup jobs [#98]

## [2.0.0a1] - 2026-08-27

This is the first public HarborRAG 2.0 alpha release. Python package metadata,
distribution filenames, dependency pins, and release tags use the canonical
PEP 440 version `2.0.0a1`; release tooling also accepts `2.0.0-alpha` as an
operator-facing alias.

### Added

#### harborrag

- HarborRAG 2.0: a modular, provider-agnostic RAG framework built from eight independently testable packages [#1] [#6]
- One `harborrag` install, with extras for the CLI, HTTP API, MCP server, chat, Temporal and each storage provider [#31]
- Python SDK with ingestion, retrieval, graph and chat services behind one async client [#32]

#### harborrag-adapters

- Connectors for local files, GitHub, Confluence, Jira and SharePoint, including comments and attachments [#2] [#6]
- Parsers for text, Markdown, HTML, JSON, CSV/Excel, Word, ODT, EPUB, PowerPoint, images and PDF [#2] [#9]
- Choice of PDF engines: PyMuPDF, Docling, LiteParse, MinerU or PaddleOCR [#2] [#9]
- Chat, embedding and reranking clients with configurable providers [#4]
- Storage on PostgreSQL, S3-compatible object storage, Qdrant, FalkorDB and Redis [#5] [#7]
- Confluence pages are converted into structured text and tables [#16]
- Optional attachment ingestion for Confluence and Jira [#19]
- Local file discovery reports skipped files with a reason [#14]

#### harborrag-app

- `harborrag` operator CLI [#1]
- HTTP control-plane API with authentication, role-based access, health checks and a standard error format [#7]
- Read APIs for projects, sources, activity, settings and metrics [#11]
- Ingestion and connection APIs with live progress streaming [#25] [#34]

#### harborrag-engine

- Ingestion pipeline that skips unchanged documents instead of re-parsing and re-embedding them [#12] [#22]
- Table-aware chunking [#15] [#16]
- Dense, sparse and hybrid retrieval that only cites the current version of a document [#12] [#27]
- Knowledge graph of document structure and source relationships [#12] [#38]
- Retrieval-grounded chat with streaming, and a bounded multi-turn agent [#22]

#### harborrag-mcp-server

- MCP server with `vector_search` and graph tools for triplets, paths and subgraphs [#10] [#22] [#27]

#### harborrag-memory

- Short-term and working memory for chat sessions [#22]

#### harborrag-runtime

- Durable ingestion on Temporal: survives worker restarts, retries single documents, and supports pause, resume and cancel [#12] [#24]
- Rebuild vector and graph indexes from stored content without re-crawling sources [#12]
- Temporal settings in YAML configuration [#32]

#### Deployment

- Docker Compose stack and `dev.sh` scripts to run the full stack locally [#22] [#32]

#### Documentation

- Public documentation website with interactive architecture and feature sections [#31] [#40]
- Data-lifecycle, runtime-reliability, publication and security guides [#31]

#### Development

- Pre-commit and pre-push hooks running the same quality gates as CI [#28]

### Fixed

#### harborrag-adapters

- CMYK images are converted before OCR [#37]
- Clearer errors for undecodable text files and password-protected presentations [#18] [#22]

#### harborrag-engine

- Retrieval never returns a superseded document version during a concurrent publish [#31]
- Graph subgraph search returns complete results and no longer times out on large graphs [#35]
- Jira subtasks no longer appear as a separate node from their parent issue [#33]
- The graph keeps page and issue membership separate from document structure across all connectors [#38]
- Relation filters only accept relation types the graph actually stores [#36]

#### harborrag-mcp-server

- The local MCP HTTP server works on Windows [#26]

#### harborrag-runtime

- Parsing falls back to in-process when a subprocess cannot start, instead of failing the document [#30]

#### Documentation

- Website build fixes for link checking, coverage pages, UTF-8 output and asset paths [#43] [#44] [#45] [#48]

### Changed

#### harborrag

- License changed to Apache 2.0 [#3]

### Security

#### harborrag-adapters

- Connector credentials are encrypted at rest [#25]
- Outbound requests to private or disallowed addresses are blocked [#8]

#### harborrag-app

- Every control-plane API call is limited to the caller's tenant [#23]
- Secrets are redacted from API responses and diagnostics [#8] [#11]
- Production refuses to run on the default SQLite database [#8]

#### harborrag-mcp-server

- The audit log refuses symlinks and other unsafe files [#26] [#27]

#### Deployment

- Database, MinIO and Grafana passwords must be supplied by the operator [#31]

[Unreleased]: https://github.com/cbtw-apac/HarborRAG/compare/harborrag-v2.0.0a2...HEAD
[2.0.0a2]: https://github.com/cbtw-apac/HarborRAG/compare/harborrag-v2.0.0a1...harborrag-v2.0.0a2
[2.0.0a1]: https://github.com/cbtw-apac/HarborRAG/releases/tag/harborrag-v2.0.0a1
