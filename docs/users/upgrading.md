# Upgrading

This page lists the steps each release needs when you upgrade an existing deployment,
newest release first. The [changelog](../../CHANGELOG.md) lists everything that changed;
entries marked **Breaking** there are the ones these steps deal with.

## 2.0.0a2 to 2.0.0

Connections move from `config/sources/connectors.yaml` into the control database and
are managed through the API ([Connections](connections.md)). Ingestion reads
each connection at the start of every run.

### Before you start

- Workers now read connections, revisions and stored secrets from the control
  database. Give every worker the same `HARBORRAG_CONTROL_DB_URL` and
  `HARBORRAG_SECRETS_ENCRYPTION_KEY` as the API, or point both at the same
  file with `HARBORRAG_SECRETS_ENCRYPTION_KEY_FILE`. A worker refuses to start
  without a key against a non-SQLite database. `scripts/deployment/dev.sh`
  generates the key when `env/.env.database` leaves it blank; keep a backup.
- The API applies migrations `0041`–`0046` at start-up. `0046` adds the
  runtime settings tables: the overrides and an append-only revision log
  ([Runtime settings](configuration/runtime-settings.md)). Start the API before
  the workers. `0044` drops the `agent_runs` table and `0045` the rest of the
  1.x agent traces: tool-call messages, replay records of agent sessions,
  run-scoped and working memories, and the session `kind`, message `run_id`
  and usage `surface` columns. A downgrade brings the columns back, with
  `kind` and `surface` set to `chat` and the rest empty; the deleted rows do
  not come back.
- Upgrade the workers together with the API. The worker records the
  cancellation of a retry run, so with an older worker a retry run cancelled
  through the API ends `failed`, or stays `running` until the API next
  settles it, for example at the next start of its connection.
- Give the API the environment variables `config/sources/connectors.yaml` names, for
  example `JIRA_BASE_URL` or `LOCAL_SOURCE_PATH`. The one-time import reads
  non-secret values from the API's environment.

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
`config/harborrag.example.yaml` is removed too: `harborrag init` renders `harborrag.yaml`
in the project root.

- A deployment that sets the path of every file it uses keeps working without moving
  anything: `HARBORRAG_CONNECTOR_CONFIG_PATH`, `HARBORRAG_SCHEDULE_CONFIG_PATH`,
  `HARBORRAG_PARSER_CONFIG_PATH`, `HARBORRAG_CHUNKING_CONFIG_PATH`,
  `HARBORRAG_GRAPH_BUILD_CONFIG_PATH`, `HARBORRAG_MODEL_CONFIG_PATH`,
  `HARBORRAG_MCP_CONFIG_PATH` and `HARBORRAG_MCP_KEYS_PATH`. The images now set the new
  paths, so a deployment that relied on their old values must move the files or set
  the variables.
- An environment file that names an old path, such as
  `HARBORRAG_MCP_CONFIG_PATH=config/mcp.yaml`, now points at a missing file. Update it.
- Some files are optional. While its path variable is unset, a missing `chunking.yaml`
  or `graph/build.yaml` means the built-in defaults, and the graph-build defaults make
  no LLM calls; a missing `mcp/server.yaml` means the packaged MCP defaults. A missing
  `schedules.yaml` always means no declared schedules. A file left at its old path is
  therefore ignored without a warning. Run `harborrag doctor` and
  `harborrag topology config validate` after the move.
- A relative `source_path` written directly in `connectors.yaml` resolves against the
  file's folder, now `config/sources/`. Add a `../` or use an absolute path.
  Environment-backed paths such as `LOCAL_SOURCE_PATH` are unaffected.
- `config/sources/schedules.yaml` and `config/mcp/keys.yaml` are git-ignored, like
  `config/settings.yaml`. Keep them in your deployment's own version control.
- The development worker now mounts the whole `config/` read-only, like the API and the
  MCP server. A configuration change needs a worker restart, not an image rebuild.

**Breaking:** `config/models/models.yaml` reads renamed environment variables. Rename
every variable with these prefixes in `env/.env.models` and any other environment file,
including those only the model smoke tests read, such as `HARBOR_CHAT_BACKEND`:

| Old prefix | New prefix |
| --- | --- |
| `HARBOR_CHAT_` | `HARBORRAG_CHAT_` |
| `HARBOR_EMBED_` | `HARBORRAG_EMBED_` |
| `HARBOR_MEMORY_` | `HARBORRAG_MEMORY_` |

```bash
sed -i -E 's/(^|[^A-Z_])HARBOR_(CHAT|EMBED|MEMORY)_/\1HARBORRAG_\2_/g' env/.env.models
```

`HARBOR_SMOKE_*` and `HARBOR_GATEWAY_TENANT` keep their names. With the old names,
loading the catalog fails with `environment variable HARBORRAG_CHAT_PROVIDER is not set`.

### What happens at the first start

The API imports `config/sources/connectors.yaml` once, when all of these hold:

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
  responses use the `Ingestion` shape described below.
- **Ingestion runs answer the explorer `Ingestion` shape**, and the task shape
  (`progress`, `stage`, `source`, `submitted_at`, `message`) is retired with no
  alias. A run carries `ingestion_id`, `tenant_id`, `project_id`,
  `connection_id`, `mode`, a lowercase `status` (`queued`, `running`, `paused`,
  `succeeded`, `failed`, `cancelled`), `counters`, `error_category`,
  `triggered_by`, `settings_revision`, `workflow_id`, `run_id`, `trace_id`,
  `links`, `created_at` and `finished_at`.
- **Ingestion reads need role `editor` and send the tenant in `X-Tenant-Id`.**
  The `?tenant_id` query parameter is removed; a run in another tenant is
  `404`. The list filters are `project_id`, `connection_ref`, `status` and
  `created_after`.
- **Pause, resume and cancel answer `200` with the run** after waiting for the
  new status; an action the state does not allow is `409 invalid_state`. A
  retry run can be cancelled but not paused or resumed: `pause` and `resume`
  on it are `409 invalid_state`.
  `retry-failures` answers `202` with the new run and its `Location`.
- **Failure inbox contract:** `GET /v1/admin/failures` leaves out `total`
  unless you send `include_total=true`, instead of answering `null`.
  `POST /v1/admin/failures/retry` answers `409 invalid_state` when no
  failure matches, instead of `202` with `ingestion_id: null`.
- **Connection `stats.errors`** is the failed-document count of the latest
  run, the same number as `counters.failed` of that run, and `0` for a
  connection that never ran; it was `null` after a cancelled run.
- **Settings schema:** `GET /v1/admin/settings/schema` answers
  `{"groups": [{"name", "title", "settings": [...]}]}` instead of a map by key;
  each setting carries `key`, `type`, `default`, `min`, `max`, `levels`,
  `applies`, `description` and, for an enumerated key, `enum`.
- **New routes:** `GET /v1/admin/failures` and `POST /v1/admin/failures/retry`
  for the failure inbox; see
  [Change, ingest and delete](connections.md#change-ingest-and-delete).
- **New settings:** `HARBORRAG_TEMPORAL_UI_URL` and `HARBORRAG_GRAFANA_URL`
  fill `links` of a run; add them to `env/.env.api`.
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

### Runtime settings

Six settings (`retrieval.top_k`, `retrieval.rerank`, `parsing.pdf_parser`,
`chunking.max_tokens`, `ingestion.concurrency`, `models.embedding`) can now be
changed through `/v1/admin/settings` ([Runtime settings](configuration/runtime-settings.md)).
Nothing changes at upgrade: a setting with no override keeps using the knob
that governs it today (`config/mcp/server.yaml`, `HARBORRAG_CHAT_RETRIEVAL_TOP_K`,
`config/ingestion/parsers.yaml`, `config/ingestion/chunking.yaml`, `config/temporal.yaml`), and no
document is reprocessed.

`GET /v1/admin/settings` shows the registry default for a setting with no
override, even when you changed its old knob. To carry a changed knob over,
create `config/settings.yaml` before the first start of the new API, with
only the keys whose old knob you changed and your value for each, for example
`retrieval.top_k: 8`. Do not copy `config/settings.example.yaml` whole: it
lists every key you can set at its registry default, and every key in the seed file
becomes an override that replaces its old knob, so a copied default would pin
the default over a value you changed. The API seeds the tenant `HARBORRAG_INGESTION_TENANT_ID`
from that file once, when the tenant has no settings revisions. Set
`HARBORRAG_SETTINGS_SEED_PATH` to read the file from another path. After the
seed, edits to the file only log a `config_yaml_drift` warning; change
settings with the API or `harborrag config import`.

The first sync after any `parsing.*` or `chunking.*` override reprocesses that
connection, because both feed the processing identity.

#### Use docling for PDFs

`parsing.pdf_parser: docling` needs two things HarborRAG does not ship:

1. A `parser: pdf` definition with a single `docling` engine in
   `config/ingestion/parsers.yaml`, added with `enabled: false` so the shipped
   `pdf-liteparse` definition stays the boot default. Copy the whole
   commented `pdf-docling:` block of `config/ingestion/parsers.yaml`, from the
   `pdf-docling:` line through its `engines` list and the last docling
   setting (`max_file_size`), into the active `parsers:` section
   uncommented, and change `enabled: true` to `enabled: false`. Keep its
   `engines` list to the single `docling` entry.
2. The `pdf-docling` extra of `harborrag-adapters` installed in the worker
   image, for example `pip install "harborrag-adapters[pdf-docling]"`.

Without them, PDF documents fail with `document_release_parser_unavailable`,
recorded as retryable. Add the definition, then run `retry-failures`; the
retry replays the parser the run was pinned to.

Adding the definition reprocesses every connection once, not only the one you
switch to docling. The parser identity is the digest of the whole
`config/ingestion/parsers.yaml`, so any edit to the file changes it for every
connection, and the next sync of each connection reprocesses all of its
documents. To keep that to one reprocess:

1. Put the same `config/ingestion/parsers.yaml` in the API and in the worker before you
   set `parsing.pdf_parser: docling`. The API reads the file when it starts a
   run and the worker when it prepares a scheduled run, so with two different
   files the identity flips between the two kinds of run and documents
   reprocess on alternate syncs; docling documents also fail with
   `document_release_parser_unavailable` on a worker without the definition.
   The dev compose mounts the same `config/` into the API and the worker, so
   restart both after editing the file.
2. Then set the override.

A `retry-failures` run replays the processing identity of the run it retries.
Documents it publishes after you add the definition keep the old identity, so
the next sync reprocesses them once more.

### Not yet supported

- `item_refs` on `POST /v1/admin/ingestions` answers `501 not_supported`.
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
2. **MCP limits.** Review `config/mcp/server.yaml`. Graph `max_depth` above 4 and `max_paths`
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
to `true` in `config/graph/build.yaml`, then run:

```bash
harborrag topology summaries backfill
```
