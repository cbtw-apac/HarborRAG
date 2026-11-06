# Upgrading

This page lists the steps each release needs when you upgrade an existing deployment,
newest release first. The [changelog](../../CHANGELOG.md) lists everything that changed;
entries marked **Breaking** there are the ones these steps deal with.

## 2.0.0a2 to 2.0.0

Connections move from `config/connectors.yaml` into the control database and
are managed through the API ([Connections](connections.md)). Ingestion reads
each connection at the start of every run.

### Before you start

- Workers now read connections, revisions and stored secrets from the control
  database. Give every worker the same `HARBORRAG_CONTROL_DB_URL` and
  `HARBORRAG_SECRETS_ENCRYPTION_KEY` as the API, or point both at the same
  file with `HARBORRAG_SECRETS_ENCRYPTION_KEY_FILE`. A worker refuses to start
  without a key against a non-SQLite database. `scripts/deployment/dev.sh`
  generates the key when `env/.env.database` leaves it blank; keep a backup.
- The API applies migrations `0041`–`0045` at start-up. Start the API before
  the workers. `0044` drops the `agent_runs` table and `0045` the rest of the
  1.x agent traces: tool-call messages, replay records of agent sessions,
  run-scoped and working memories, and the session `kind`, message `run_id`
  and usage `surface` columns. A downgrade brings the columns back, with
  `kind` and `surface` set to `chat` and the rest empty; the deleted rows do
  not come back.
- Give the API the environment variables `config/connectors.yaml` names, for
  example `JIRA_BASE_URL` or `LOCAL_SOURCE_PATH`. The one-time import reads
  non-secret values from the API's environment.

### What happens at the first start

The API imports `config/connectors.yaml` once, when all of these hold:

- the file exists;
- the install has no projects and no connections, in any tenant;
- no earlier import was recorded.

It imports into the tenant `HARBORRAG_INGESTION_TENANT_ID` names (`DEFAULT`
unless set). An install upgraded with projects or connections already stored,
including rows carried over from the old `/api/v1/sources` routes, is not
imported: add its connections through the API or `harborrag source add`.

The import runs once. A failed import is recorded and not tried again; a project
it created that holds no connection is removed, and what it missed is added
through the API. An import stopped part way, for example by a restart, resumes
at the next start.

Each entry becomes a connection:

- The entry's key becomes the connection's `source_key`, so its documents keep
  their ids and source scopes. The key is also its `connection_id` when it is a
  valid id (lowercase letters, digits, `-` and `_`); otherwise a slug of it is.
- `settings` and `environment` values are split into credentials and config.
  Environment values are read at import time.
- `secrets: <field>_env: NAME` becomes the reference
  `{"secret_ref": "env:NAME"}`. The value stays in the worker's environment.
  Such a reference keeps working; through the API, an editor can keep it or
  replace it with a value, but cannot point a field at another variable.
- Tuning such as `requests_per_minute` and `page_size` is kept on the
  connection and shown as its `advanced` settings.
- An entry with `project_id` goes to that project; others go to `default`,
  created when missing.
- A disabled entry is skipped. An entry with a missing environment variable or
  an invalid value is imported with status `error` and its field issues; fix it
  with `PUT`. A Jira or Confluence Cloud entry keeps the `api_token` method
  without its email, so the `PUT` only adds the `email`.

The plan is logged and the import is recorded in the workspace settings. The
file is never read again: later changes go through the API. `harborrag init`
and `harborrag doctor` keep working on the file, and a fresh install made with
`harborrag init` is imported at its first start the same way. In direct mode,
`harborrag ingest` runs the same import before its first run.

### Rows from `/api/v1/sources`

Existing rows become connections. Each gets a `connection_id` slugged from its
name, made unique in its project with `-2`, `-3` and so on, and the
`source_key` `<project_id>/<connection_id>`. A `paused` row becomes `active`;
a row whose type is unknown becomes `error`.

A Jira or Confluence row without an email gets the `pat` method, which a Cloud
site cannot use: starting its ingestion answers `422` on `credentials.email`.
`PUT` the connection with `"method": "api_token"` and the `email`; the stored
token is kept.

### Behaviour that changes

- **SharePoint delegated sign-ins move under the HarborRAG tenant.** Without a
  `token_cache_path`, the refresh token now lives in
  `~/.harborrag/sharepoint/<harborrag-tenant>/token-<tenant>-<client>.json`
  instead of one file shared by every tenant. Sign in again with
  `scripts/deployment/dev.sh sharepoint-login` (or the `login` command with
  `--harborrag-tenant`) before the next sync; connections with an explicit
  `token_cache_path` are unaffected.
- **Configuration fingerprints change once.** Runs now check a fingerprint of
  the stored connection. Retrying the failures of a run started before the
  upgrade reports a configuration difference; start a new sync instead.
- **A change applies from the next run, on every worker, without a restart.**
  A run that is already going keeps the values it started with.
- **Editing a Jira connection's `jql` keeps its source scope**, so items the
  new query no longer matches are removed by the next full scan.
- **`GET /v1/admin/connections` lists stored connections** in the caller's
  tenant. The `source_type` for local files is `local_file`, and
  `POST /v1/admin/ingestions` starts one by its `project_id` and
  `connection_id`.
- **Request settings can be edited.** A connection's `advanced` object holds
  `requests_per_minute`, `page_size`, `request_timeout_seconds`,
  `max_retries` and `backoff_factor`; see
  [Advanced settings](connections.md#advanced-settings).
- **Every connector reads a fixed list of file types by default**: `md`,
  `markdown`, `txt`, `pdf`, `docx`, `doc`, `epub`, `odt`, `pptx`, `csv`,
  `xlsx`, `png`, `jpg`, `jpeg` and `html`. This covers Jira and Confluence
  attachments, which can now be filtered with `allowed_extensions` and
  `excluded_extensions`. A connection that does not set `allowed_extensions`
  stops reading other types, and its next full sync removes documents already
  read from them, such as source code in a GitHub repository. Set
  `"allowed_extensions": []` to keep every type.
- **Documents above 20 MiB are skipped by default.** A connection that sets
  `max_file_size_bytes` or `max_attachment_size_bytes` keeps its own value.
- **Parsers refuse oversized documents**: PDFs above 200 pages, images above
  25 megapixels, and spreadsheets above 50,000 rows or 200,000 cells. See
  [Document limits](connections.md#document-limits).
- **Connectors reach public addresses only.** A Jira or Confluence Data
  Center, GitHub Enterprise or other host on a private, loopback or link-local
  address is refused until its host name or network is listed in
  `HARBORRAG_CONNECTOR_ALLOWED_HOSTS`, in both `env/.env.connector` and
  `env/.env.api`. See [Network access](connections.md#network-access).
- **`env:` references come only from `connectors.yaml` and the CLI.** The
  HTTP API takes secret values and refuses a new `env:NAME`.
- **Local paths can be restricted.** With `HARBORRAG_LOCAL_SOURCE_ROOT` or
  `LOCAL_SOURCE_PATH` set, a `local_file` connection must stay inside it.
  Without either on the API, the API refuses new `local_file` connections;
  the CLI and `connectors.yaml` still accept them.
- **Removed:** `GET /v1/connections` and `/api/v1/sources*` answer `404`.

### HTTP API changes

- **Removed routes answer `404`, with no deprecated alias:** every `/api/v1/*`
  route, including `/api/v1/health`, `/api/v1/readyz` and `/api/v1/metrics`,
  and `/v1/connections`, `/v1/agent/*`, `/v1/retrieval/*`, `/v1/graph/*`,
  `/v1/memory/*`, `/v1/schedules/*`, `/v1/admin/projections/*` and
  `PATCH`/`DELETE /v1/chat/sessions/{session_id}`. MCP server tools replace
  agent and retrieval access.
- **Probes and docs moved to the root:** `/healthz`, `/readyz`, `/docs` and
  `/openapi.json`.
- **`/metrics` answers on the ops port** (`HARBORRAG_OPS_PORT`, 9465), only to
  `HARBORRAG_METRICS_SCRAPE_TOKEN`; the API image starts with
  `python -m harborrag_app.api.serve`.
- **Every error uses one catalog of codes**, such as `not_found`,
  `validation_error`, `unauthenticated` and `rate_limited`, in place of codes
  like `harbor_not_found_error`. `details` is `null`, an object, or for
  `validation_error` a list of `{field, issue}`.
- **The trace header is `X-Trace-Id`**, 32 lowercase hex characters, in place
  of `X-Request-Id`.
- **Each principal is rate-limited**: `HARBORRAG_HTTP_RATE_DEFAULT` (10) calls
  per second on `/v1`, and `HARBORRAG_HTTP_RATE_CHAT` (2) chat completions per
  second, answering `429` with `Retry-After`.
- **A credential with several tenants sends `X-Tenant-Id`** on admin routes,
  else `400 tenant_required`; a tenant it does not hold is
  `403 tenant_forbidden`.
- **An `Idempotency-Key` reused with another body is `422`**, not `409`; a key
  is kept for 24 hours.
- **Ingestions start at `POST /v1/admin/ingestions`** with `project_id` and
  `connection_id` in the body. `POST …/connections/{connection_id}/sync`,
  `POST /v1/ingestions` and the `sync_now` field of connection `PUT` are
  removed.
- **Ingestion runs moved to `/v1/admin/ingestions`:** `/v1/ingestions*` answers
  `404`; `{task_id}` is now `{ingestion_id}` and `/stream` is `/events`. The
  responses keep the task shape they had, with the identifier names below.
- **Identifiers are named after what they identify**, never a bare `id`, and
  the old names are not accepted:
  - a project's `id` is `project_id`, and its `name` and `description` are
    `project_name` and `project_description` in the responses and the `PUT`
    body.
  - a connection's `ref` is `connection_ref` and its `last_ingestion.id` is
    `last_ingestion.ingestion_id`; the reader filter chips return
    `connection_ref`.
  - the started ingestion returns `ingestion_id`, its `connection` ref is
    split into `tenant_id`, `project_id` and `connection_id`, and
    `triggered_by` is `{principal_type, principal_id}`. `workflow_id` and
    `run_id` keep their names.
  - ingestion reads and controls return `ingestion_id` and `tenant_id` in
    place of `task_id` and `tenant`, the reads return the connection's
    `source.source_key` in place of `source.connection_id`, retry-failures
    returns `retry_ingestion_id`, and the listing filter is `?tenant_id`.
  - a source type's `auth_methods[].id` is `auth_method_id`, and a connection
    test's `preview.sample[].ref` is `item_ref`.
  - `GET /v1/me` returns `principal.principal_type` and
    `principal.principal_id`.
  - a chat completion returns `completion_id`; chat bodies send `tenant_id`
    and the session listings take `?tenant_id`.
  - a project delete refused with `409` names `details.connection_refs`, or
    `details.connection_ref` for a connection that is ingesting.
- **Chat completions are retrieval chat only:** `max_steps` is removed and
  answers `422`, the `response.agent.progress` stream event is gone, and
  `HARBORRAG_API_AGENT_TOKEN_BUDGET` and `HARBORRAG_MEMORY_AGENT_TOOLS` are
  ignored. Sessions created by the 1.x agent surface stay stored and are now
  listed by `GET /v1/chat/sessions` like any chat session. Session listings
  no longer carry `kind`, stored messages no longer carry `run_id`, and the
  `tool` message role is gone.

### Not yet supported

- `item_refs` on `POST /v1/admin/ingestions` answers `501 not_supported`.
- The ingestion read and control routes still return the task shape
  (`ingestion_id`, `tenant_id`, a `tenant_id` query filter), not the
  `Ingestion` shape the start route returns.
- Project ids are unique across tenants.

## 2.0.0a1 to 2.0.0a2

Alpha2 keeps each tenant's data under one storage namespace across Qdrant, FalkorDB and the
object store, and it changes how documents are projected into the graph. Stored data is not
migrated, so plan a reset and a full re-ingest.

### 1. Before you upgrade

1. Stop schedules and API submissions. Let every running HarborRAG workflow finish, or
   terminate them:

   ```bash
   temporal workflow terminate --query 'WorkflowType STARTS_WITH "harborrag"'
   ```

   An alpha1 run resumed by an alpha2 worker cannot find its plans and artifacts.

2. Back up the PostgreSQL control database. Restoring that backup, together with the
   untouched alpha1 stores, is the only practical rollback: alpha1 cannot start against a
   migrated database, and downgrading the schema loses data.

### 2. Update configuration

1. **Storage namespace.** Unset `HARBORRAG_QDRANT_COLLECTION_PREFIX` and
   `HARBORRAG_FALKORDB_TENANT_GRAPH_PREFIX`; they are ignored with a startup warning. Set
   `HARBORRAG_STORAGE_NAMESPACE_PREFIX` instead (default `harborrag`), and give each
   deployment its own value when they share stores. See
   [One tenant namespace in every store](configuration/workspace-mode.md#one-tenant-namespace-in-every-store).
2. **MCP limits.** Review `config/mcp.yaml`. Graph `max_depth` above 4 and `max_paths`
   above 5 are lowered with a warning.
3. **Confluence.** Remove `max_child_pages` from Confluence connectors; it is ignored with a
   warning.
4. **SharePoint.** Grant the app `Sites.Selected` on the configured sites only. A token with
   a broad role such as `Sites.Read.All` is refused at startup. Only `docx`, `pdf`, `pptx`,
   `xlsx`, `txt` and `md` files are ingested by default; set `allowed_extensions` to add
   others. For `auth_mode: device_code` on the Docker stack, rebuild the worker image, then
   run `scripts/deployment/dev.sh sharepoint-login`. See
   [SharePoint crawl scope, allowlists, and skips](configuration/connector-config.md#sharepoint-crawl-scope-allowlists-and-skips).

### 3. Clear the alpha1 data

Drop the alpha1 Qdrant collections, FalkorDB graphs and object-store keys, or keep them until
the upgrade is confirmed if you may need to roll back. Alpha2 uses these names:

| Store | Name |
| --- | --- |
| Qdrant collection | `{prefix}_{tenant}_{index}` |
| FalkorDB graph | `{prefix}_{tenant}` |
| Object-store keys | under `{prefix}_{tenant}/` |

### 4. Migrate the control database

Every HarborRAG process that opens the control database applies pending migrations at
startup. Start one process first (for example the API), wait until it is healthy, then start
the worker and the other services. The schema moves from revision 0019 to 0040; on a large
control database, migrations 0021 and 0037 hold table locks for a while.

### 5. Re-ingest

Re-ingest every source. The graph projection changed, so every document is reprocessed
without `--force-reprocess`.

Jira filter keys for custom fields with accented names have changed. Update saved filters
that use them.

### 6. Optional: keep entity summaries

Summarization is now off by default. To keep entity summaries, set `summarization.enabled`
to `true` in `config/topology/graph_build.yaml`, then run:

```bash
harborrag topology summaries backfill
```
