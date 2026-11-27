# Update HTTP and MCP clients for 2.0.0

Use this reference to update clients that call HarborRAG over HTTP or MCP. It also covers saved evidence filters and changes to chat sessions, completions and events.

Review your integrations before following the [upgrade checklist](upgrading.md#200a2-to-200).
For command and Python changes, see [Update automation for 2.0.0](upgrading-2-0-0-automation.md).

- [MCP server](#mcp-server) and [evidence filters](#evidence-filters)
- [HTTP routes, errors and limits](#routes-errors-and-limits) and [source access](#access-under-source_acl)
- [Ingestions and failures](#ingestions-and-failures) and [settings schema](#settings-schema)
- [Chat](#chat)

## MCP server

- **The MCP server reads with read-only accounts.** The Compose MCP server and Explorer
  UI server connect to Postgres as `harborrag_mcp`, which may only `SELECT` the corpus
  tables, `mcp_api_keys` and the settings tables, in read-only sessions, and to MinIO as
  `harborrag-mcp`, which may only read `harborrag-artifacts`. Neither gets the Postgres
  owner or the MinIO root credentials any more. A preflight failure now ends with the
  class of the error behind it, such as
  `settings store is unreachable (cause: InvalidPasswordError)`: run
  `scripts/deployment/dev.sh mcp-access`, then start the server again. See
  [Read-only database and object-store accounts](detailed-guides/mcp-server/server-operations.md#read-only-database-and-object-store-accounts).
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
  [Checks before the HTTP server starts](detailed-guides/mcp-server/server-operations.md#checks-before-the-http-server-starts).
- **Rebuild the MCP image once.** Run
  `scripts/deployment/mcp.sh --build --check` so the image records the commit
  it was built from. The wrapper then says when the image is older than the
  source.
- **The status page cannot save in the container.** `config/` is mounted
  read-only there, so **Save** is disabled and `PUT /api/config` answers `409`.
  Edit `config/mcp/server.yaml` and select **Reload YAML**.

## Evidence filters

- **A document's last update time is called `last_modified`.** Evidence is
  stored under that name, and the `filters` of the MCP `vector_search` tool and
  of chat requests take `last_modified` instead of `updated_at`. A filter on
  `updated_at` is now refused. Update saved filters and agent prompts that name
  `updated_at`. Stored documents keep the old name until a `force` re-ingest
  reprocesses them, because an ordinary run skips unchanged documents. Run the
  one in
  [Re-index when the embedding model changes](upgrading-2-0-0-configuration.md#re-index-when-the-embedding-model-changes).

## HTTP API changes

### Routes, errors and limits

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
  [Check the settings that are now refused](upgrading-2-0-0-configuration.md#check-the-settings-that-are-now-refused).
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

### Access under `source_acl`

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

### Ingestions and failures

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
      outages, a missing parser or cancellations at the parse stage;
    - a local file replaced by a FIFO, a socket or a device after discovery fails as
      `document_unsupported`, which is not retried.
- **A refused source request is stored as a configuration failure.** A source failure
  that is not retried because the source refused the request, such as an HTTP `400`,
  `410` or `422` or a refused response, is stored with the category
  `INVALID_CONFIGURATION` instead of `TRANSIENT` in the control database's
  `document_failures.failure_category`. Its code stays `source_unavailable`, so the
  API's `error_category` does not change. Rows stored before the upgrade keep
  `TRANSIENT`, so a query or dashboard that groups by the stored category sees the
  same failure under two categories.
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

### Settings schema

- `GET /v1/admin/settings/schema` answers
  `{"groups": [{"name", "title", "settings": [...]}]}` instead of a map by key. Each
  setting carries `key`, `type`, `default`, `levels`, `applies` and `description`, plus
  `min` and `max` for a bounded key and `enum` for an enumerated key.

### Chat

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
  question; see [Update scripts for CLI v2.0.0](upgrading-2-0-0-automation.md#update-scripts-for-cli-v200).
- **SDK callers of the chat service** (`harborrag_app.workflow_control`, now
  `harborrag_runtime.application`; see
  [Python import paths and service methods](upgrading-2-0-0-automation.md#python-import-paths-and-service-methods)):
    - `ChatExecutionOptions.session_id` is optional (`None` asks a one-off question),
      `project_id` is replaced by `project_ids` (one-off questions only), and `mode`,
      `connections`, `filters` and `trace_id` are new.
    - `chat_completion()` answers the explorer completion body and raises the turn's
      errors instead of returning a failed response for them; the new `chat_answer()`
      yields the turn's events (`RetrievalFound`, `AnswerDelta`, `TurnCompleted`).
    - `validate_chat_model`, `validate_chat_project` and `validate_completion_scope` are
      removed: the turn checks the model, the projects and the request's scope itself.
