# Connections

A connection is one configured data source in a project: where and how to
connect (`credentials`) and what to sync (`config`). Connections live in the
control database and are managed through the HTTP API. Every ingestion worker
reads the connection at the start of each run, so a change applies from the
next run on every worker, with no restart.

A connection has four parts: `credentials` (how to sign in), `config` (what
to sync), `advanced` (request rate, paging, timeouts and retries) and a `name`.

`config/sources/connectors.yaml` is read once, at the first start of a fresh install
with no projects and no connections, and imported into the tenant
`HARBORRAG_INGESTION_TENANT_ID` names. After that, change connections through the
API. See [Upgrading](upgrading.md).

## Add a connection in three calls

The form an operator fills in maps to three calls. All of them need the
`editor` role. To fill the body in a browser instead, see
[Swagger UI templates](#swagger-ui-templates). From a terminal, `harborrag source`
does the same through the same service; see the
[CLI reference](cli-reference/README.md#connections).

**1. Pick the data source.** List the source types and their fields:

```bash
curl -H "Authorization: Bearer $TOKEN" https://harborrag.example.com/v1/admin/source-types
```

Each type returns its `auth_methods` (one JSON Schema and one set of `ui` form
hints each), a `config_schema`, an `advanced_schema`, `ui` hints for the config
and advanced fields, and its `capabilities`. A field with `writeOnly: true` and
`ui.widget: secret` is a secret. When a type has several auth methods, each
method's schema has a `method` property whose `const` is the value to send.
The config and advanced schemas publish each field's `default`: the value the
connector uses when the field is left out. `ui.widget` is `text`, `secret`,
`select`, `textarea`, `path`, `list`, `toggle` or `number`.

URL fields that carry credentials (`base_url`, `api_url`, `web_url`,
`graph_api_url`, `site_url`) must be `https://`. GitHub needs
`repository_url`, or `owner` and `repo`; SharePoint needs `site_url`,
`site_id`, or `hostname` and `site_path`. The schemas state these rules with
`pattern`, `anyOf` and `allOf`, and a `PUT` or `test` that breaks them, or any
other rule the connector checks, answers `422` naming the field.

| `source_type` | Category | Setup | `credentials.method` |
| --- | --- | --- | --- |
| `jira` | issue_tracker | Jira Cloud: email and API token | `api_token` |
| | | Jira Data Center: personal access token | `pat` |
| `confluence` | wiki | Confluence Cloud: email and API token | `api_token` |
| | | Confluence Data Center: personal access token | `pat` |
| `sharepoint` | files | App registration: client secret | `app` |
| | | Device-code sign-in | `oauth` |
| `local_file` | files | A folder the workers can read | `none` |
| `github` | code | Public repository, no token | `none` |
| | | Private repository: access token | `pat` |

Pick the method with `credentials.method`, for example `"method": "pat"`.
Without it, the first method listed for the type applies, so send it for any
other method. SharePoint also
accepts `pat`, a Microsoft Graph access token, but it expires within about an
hour, so it suits a one-off test rather than syncs.

GitHub is read through its REST API over HTTPS, so it signs in with a token,
not an SSH key. An SSH URL such as `git@github.com:acme/handbook.git` is
accepted as `repository_url`; only its owner and name are used. Without a
token, GitHub allows 60 requests an hour, which a large repository exceeds.
For GitHub Enterprise Server, set `credentials.api_url` and
`credentials.web_url`.

SharePoint device-code sign-in reads as a user who signed in once with
`python -m harborrag_adapters.connectors.sharepoint.login --tenant-id … --client-id … --harborrag-tenant …`
on a host whose `token_cache_path` the workers can also read. Without a
`token_cache_path`, the sign-in is kept per HarborRAG tenant, so pass the
connection's tenant to `--harborrag-tenant`. The API cannot
show the sign-in prompt itself. `token_cache_path` is a path on the worker, so
only `config/sources/connectors.yaml` and the `harborrag source` CLI can set or change
it; through the API, leave it as read or leave it out when it was never set.

**2. Test it.** Send the form values; nothing is saved:

```bash
curl -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  https://harborrag.example.com/v1/admin/projects/cpm/connections/cpm-tickets/test \
  -d '{"source_type": "jira",
       "credentials": {"base_url": "https://acme.atlassian.net",
                       "email": "bot@acme.com", "api_token": "ATATT3x..."},
       "config": {"jql": "project = CPM"}}'
```

The answer is always `200` for valid input. `ok` is `false` when a check fails,
and `error_category` says why (`auth_invalid`, `forbidden`, `unreachable`,
`rate_limited`, or `secret_unavailable` when a token is an `env:` reference that
only the worker can read). Up to five sample items show what a sync would read. A test
gives up after 15 seconds. Without a body, the call tests the saved connection.

**3. Save it.** `PUT` the same body:

```bash
curl -X PUT -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  https://harborrag.example.com/v1/admin/projects/cpm/connections/cpm-tickets \
  -d '{"source_type": "jira", "name": "CPM tickets",
       "credentials": {"base_url": "https://acme.atlassian.net",
                       "email": "bot@acme.com", "api_token": "ATATT3x..."},
       "config": {"jql": "project = CPM"}}'
```

**4. Start the first ingestion.** Saving a connection never starts one:

```bash
curl -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  https://harborrag.example.com/v1/admin/ingestions \
  -d '{"project_id": "cpm", "connection_id": "cpm-tickets"}'
```

`201` means it was created, `200` that it was overwritten. There is no
separate create call: a `PUT` to a new `connection_id` creates it. The project
must exist: an admin creates it with `PUT /v1/admin/projects/{project_id}`.
Use the project's id, such as `default`, not its display name; a write that
names a project by its display name answers
`404 project 'Default' not found; did you mean 'default'?`.

## Swagger UI templates

The API's interactive documentation, at `/docs`, offers a complete
request body for each setup in the table above. Open **Create or replace a
connection** (or **Test a connection**), select **Try it out**, and pick a
template from the list Swagger UI labels **Examples**:

| Template | Key |
| --- | --- |
| Jira Cloud | `jira-cloud` |
| Jira Data Center | `jira-datacenter` |
| Confluence Cloud | `confluence-cloud` |
| Confluence Data Center | `confluence-datacenter` |
| SharePoint app registration | `sharepoint-app` |
| SharePoint device-code sign-in | `sharepoint-device_code` |
| Local files | `local_file-folder` |
| GitHub public repository | `github-public` |
| GitHub private repository | `github-token` |

Each template sets `credentials.method` and lists every `config` and
`advanced` field with the value a sync uses when you leave it as is: the
connector's own defaults, such as `max_comments: 1000` for Jira or the six
default file types for SharePoint. Replace the placeholder values, such as
`bot@acme.com` and `ATATT3x…`, then run it. `project_id` is filled with
`default`; type a new `connection_id` to create a connection.

To edit a saved connection, read it with **Get a connection** and copy its
`credentials`, `config` and `advanced` into the `PUT` body with `source_type`
and `name`. The other fields of the response, such as `status` and
`created_at`, are rejected in a request. A secret reads back as its status;
leave it out, or paste the status back, to keep the stored value.

## Change, ingest and delete

| Call | What it does |
| --- | --- |
| `GET /v1/admin/connections` | Every connection in the tenant, needing attention (`needs_reauth`, `error`) first. Filter with `project_id`, `source_type`, `status`; page with `limit` and `cursor`. |
| `GET …/connections/{connection_id}` | One connection. Secrets come back as their status, never the value. The `ETag` header is the version. |
| `PUT …/connections/{connection_id}` | Creates or overwrites the connection. `config` is replaced whole, and so is `advanced` unless it is left out. Send `If-Match` with the `ETag` you read to fail with `412` if someone changed it since, `If-Match: *` to update only, or `If-None-Match: *` to create only. `source_type` cannot change (`409 immutable_field`, `details` `{"field": "source_type"}`). A `PUT` that changes nothing stores nothing: the `ETag` and `updated_at` stay the same. Saving never starts an ingestion. |
| `POST /v1/admin/ingestions` | Starts an `incremental` or `force` ingestion of the connection named by `project_id` and `connection_id` in the body, and answers `202` with the ingestion and its `Location`. A second start while one runs answers `409 already_running` with the running ingestion's id; an unknown project or connection is `422 reference_not_found`; a connection whose stored values fail its source type's schema is `422 validation_error` with each failing field, and nothing starts. |
| `GET /v1/admin/ingestions` | Runs in the tenant, newest first. Filter with `project_id`, `connection_ref` (`project_id/connection_id`), `status` (`queued`, `running`, `paused`, `succeeded`, `failed`, `cancelled`) and `created_after` (a date-time with a UTC offset, such as `2026-11-01T00:00:00Z`; without one it is `422`); page with `limit` (default 20, 1 to 100) and `cursor`, which is bound to the filter it came from. Needs role `editor` and `X-Tenant-Id`; a run in another tenant is `404`. |
| `GET /v1/admin/ingestions/{ingestion_id}` | One run: `ingestion_id`, `tenant_id`, `project_id`, `connection_id`, `mode`, `status`, `counters` (`discovered`, `processed`, `skipped`, `failed`), `stages` (empty), `error_category`, `triggered_by`, `settings_revision`, `workflow_id`, `run_id`, `trace_id`, `links` (`temporal`, `grafana`, `langfuse`), `created_at`, `finished_at`. |
| `POST …/ingestions/{ingestion_id}/pause`, `/resume`, `/cancel` | Answer `200` with the run after waiting for the new status: up to 10 s for pause and resume, 30 s for cancel. If the wait ends first, the answer is the run as it stands. `pause` is allowed from `queued` or `running`, `resume` from `paused`, `cancel` from `queued`, `running` or `paused`; any other state is `409 invalid_state` and the run is unchanged. A retry run, started by `retry-failures` or `POST /v1/admin/failures/retry`, can be cancelled but not paused or resumed: `pause` and `resume` on it are `409 invalid_state`. Cancelling a run whose Temporal workflow was already terminated records it as `cancelled`, and so does cancelling a retry run whose workflow is gone. |
| `POST …/ingestions/{ingestion_id}/retry-failures` | Starts a new run for the documents that failed in a `succeeded`, `failed` or `cancelled` run with `counters.failed` above 0 (otherwise `409 invalid_state`). Answers `202` with the new run and its `Location`. |
| `GET …/ingestions/{ingestion_id}/documents` | One item per document: `document_ref`, `document_id`, `status` (`succeeded`, `skipped` or `failed`), `stage` (`capture`, `interpret`, `commit`, `derive` or `publish`), `error_category` and `duration_ms` (`null` for now). Filter with `status=all` (default), `succeeded`, `skipped` or `failed`; page with `limit` (default 20, 1 to 100) and `cursor`. |
| `GET /v1/admin/failures` | The failure inbox: each document's latest failed result in the tenant. Filter with `project_id`, `connection_ref`, `error_category` (`unknown` matches failures without a code), `stage` (`capture` also matches stages with no mapping), `q` and `resolved` (default `false`; a document is resolved once a later result for it succeeded, was unchanged, skipped or removed). The response carries `counts` per `error_category` over the whole filter; add `include_total=true` for `total`. Page with `limit` and `cursor`. |
| `POST /v1/admin/failures/retry` | Retries unresolved failures: the body has exactly one of `ids` (failure ids) or `filter` (`connection_ref`, `error_category`, `stage`), else `422 validation_error`. It starts one retry run per original run and answers `202` with `{"ingestion_id", "ingestion_ids", "queued"}`: the first new run, every new run, and the number of documents queued. Runs that cannot be retried are skipped and only started runs are reported. When nothing is selected, `ingestion_id` is `null`, `queued` is `0` and no run starts. |
| `DELETE …/connections/{connection_id}` | Answers `202` with a `job_id`. In the background, the connection's documents leave Qdrant and the graph, then its stored secrets are deleted, then the row. `purge=true` also drops its Postgres history. |
| `GET /v1/projects/{project_id}/connections` | Readers: the project's connections for filter chips. |

A running ingestion keeps the values it started with: a change applies from
the next run.

## Secrets

A secret field accepts a value or a reference:

| You send | What is stored |
| --- | --- |
| `"api_token": "ATATT3x..."` | The value, encrypted in the control database. Sending the value already stored keeps it, and clears a `needs_reauth` status. |
| `"api_token": {"secret_ref": "env:JIRA_TOKEN"}` | A reference: the worker reads `JIRA_TOKEN` from its own environment when the run starts. Only `config/sources/connectors.yaml` and the `harborrag source` CLI can set one; the API answers `422` and asks for the value. |
| The field left out, or its status sent back | The stored value or reference is kept. Re-saving a form without the key keeps it. |
| `"api_token": null` | Refused with `422` and `required`; the stored value is kept. Every secret is required by its auth method, so switch `method` to drop it. |

A stored secret is kept only for the same auth method and the same endpoint.
Switching `method` drops the old method's secrets, and they are deleted like a
replaced secret. Changing `base_url`, `api_url`, `web_url` or `graph_api_url`
without sending the secret again answers `422` with
`send it again: base_url changed`, so a stored secret is never sent to a new
host.

Reads return a status instead of the value:

```json
"api_token": {"set": true, "last4": "9f2c", "updated_at": "2026-10-28T09:14:03Z", "from": "stored"}
```

`last4` is `null` for references and for values shorter than eight characters.
A replaced stored secret is deleted once no running ingestion still uses it.
Secrets never appear in responses, logs, error details, exports or workflow
history: workflows carry references, and the worker resolves them.

Stored values are encrypted with `HARBORRAG_SECRETS_ENCRYPTION_KEY`, which the
API and every worker must share. `scripts/deployment/dev.sh` generates it in
`env/.env.database` when it is blank. In production, mount it as a file, such
as a Docker or Kubernetes secret, and set `HARBORRAG_SECRETS_ENCRYPTION_KEY_FILE`
to its path. Back the key up: without it, stored secrets cannot be read and
must be entered again.

## Network access

Connectors, and connection tests run by the API, reach public addresses only.
A connection whose host resolves to a private, loopback, link-local or
reserved address, such as `10.0.0.5`, `172.18.0.1`, `127.0.0.1` or the cloud
metadata address `169.254.169.254`, fails its `reachable` check with a
message naming the address. The check runs on the address each connection
actually reaches, so a DNS answer that changes later is caught too.

To reach a host inside your network, such as Jira Data Center, list its host
name or network in `HARBORRAG_CONNECTOR_ALLOWED_HOSTS`, in both
`env/.env.connector` (workers) and `env/.env.api` (tests):

```bash
HARBORRAG_CONNECTOR_ALLOWED_HOSTS=jira.acme.internal,10.20.0.0/16
```

Connectors ignore `HTTP_PROXY` and `HTTPS_PROXY` and connect directly, so a
proxy variable never switches these checks off. To send connector traffic
through a proxy, also set `HARBORRAG_CONNECTOR_USE_PROXY=true` in both env
files. HarborRAG then checks the target host's own addresses before handing
the request to the proxy, which decides where it finally goes.

## Advanced settings

`advanced` controls how HarborRAG calls the source. Its fields come from the
type's `advanced_schema`:

| Field | Types | Connector default |
| --- | --- | --- |
| `requests_per_minute` | Jira, Confluence, SharePoint, GitHub | 60 (Jira, Confluence), 120 (SharePoint, GitHub) |
| `page_size` | Jira, Confluence, SharePoint | 50 (Jira), 25 (Confluence), 200 (SharePoint) |
| `request_timeout_seconds` | Jira, Confluence, SharePoint, GitHub | 30, 60 for SharePoint |
| `max_retries` | Jira, Confluence, SharePoint, GitHub | 3 |
| `backoff_factor` | Jira, Confluence, SharePoint, GitHub | 0.5 |

Local files have no advanced settings. `advanced` stays on the connection; the settings store holds only the keys of [Runtime settings](configuration/runtime-settings.md).

| You send | Result |
| --- | --- |
| `advanced` left out | The stored values are kept. |
| `"advanced": {"page_size": 20}` | Replaces the stored values; other fields use the connector default. |
| `"advanced": {}` | Every field uses the connector default. |
| `"advanced": {"page_size": null}` | That field uses the connector default. |

Jira and Confluence accept a `page_size` from 1 to 100. Settings imported
from `connectors.yaml`, such as `requests_per_minute: 600`, show up here.

## File type filters

`config.allowed_extensions` keeps only the listed file types, and
`config.excluded_extensions` drops the listed ones. Write them with or without
the dot and in any case: `pdf`, `.PDF`. An excluded type wins over an allowed
one.

When `allowed_extensions` is left out, every source type keeps the same
document and image types: `md`, `markdown`, `txt`, `pdf`, `docx`, `doc`,
`epub`, `odt`, `pptx`, `csv`, `xlsx`, `png`, `jpg`, `jpeg` and `html`. An
explicit empty list, `"allowed_extensions": []`, keeps every type, except for
SharePoint, which needs at least one.

| `source_type` | What the filters apply to |
| --- | --- |
| `jira`, `confluence` | Attachments only; issues and pages are always read |
| `sharepoint` | Files in the document libraries |
| `local_file`, `github` | Files |

The default list leaves out source code, so a GitHub connection that should
read `.py` or `.yaml` files lists them, or sends `[]`.

For Jira and Confluence, the type comes from the attachment's file name, or
from its media type when the name has none, so `allowed_extensions: [pdf]`
keeps a PDF called `report`. An attachment the filter drops is not downloaded.

```json
"config": {"project_keys": ["CPM"], "include_attachments": true,
           "allowed_extensions": ["pdf", "docx"], "excluded_extensions": ["exe"]}
```

Changing a filter keeps the connection's source scope. The next full sync
removes documents for files the filter now drops; an incremental sync leaves
them in place.

## Document limits

Each connection skips a file or attachment larger than its size limit, before
downloading it. When the connection does not set one, the limit is 20 MiB
(20,971,520 bytes):

| `source_type` | Field |
| --- | --- |
| `jira`, `confluence` | `config.max_attachment_size_bytes` |
| `sharepoint`, `local_file`, `github` | `config.max_file_size_bytes` |

A connection can set a different size, and connections imported with a larger
size keep it. GitHub never reads a file above its 100 MB blob limit.

The parsers then refuse a document that is too large to read, whatever the
connection says. The document is reported as failed and is not retried:

| Document | Limit |
| --- | --- |
| PDF | 200 pages |
| Image | 25 megapixels |
| Spreadsheet (`xlsx`, `xls`, `csv`) | 50,000 rows or 200,000 cells per file |
| Office and EPUB archives (`docx`, `pptx`, `xlsx`, `odt`, `epub`) | 256 MiB unzipped, 5,000 members, 100:1 compression per member |

The PDF and image limits are set in `config/ingestion/parsers.yaml`
([Parser Configuration](configuration/parser-config.md)).

## Tenants

The tenant comes from the token. A token that holds several tenants sends
`X-Tenant-Id` to pick one: without it the call fails with
`400 tenant_required`, and a tenant the token does not hold gives
`403 tenant_forbidden`.

## Local files

A `local_file` connection reads a path on the worker. When
`HARBORRAG_LOCAL_SOURCE_ROOT` is set, or otherwise `LOCAL_SOURCE_PATH`,
`source_path` must be an absolute path inside that directory; a symbolic link
out of it is refused too. Set the same value on the API and the workers.
Without either, the API refuses `local_file` connections with `422` on
`config.source_path`, since any file the workers can read would otherwise be
accepted; `config/sources/connectors.yaml` and the `harborrag source` CLI can still add
them.

## Connection status

| Status | Meaning |
| --- | --- |
| `active` | The connection works. |
| `needs_reauth` | A run's credentials were rejected. Fix them with `PUT`; the next run that authenticates sets it back to `active`. |
| `error` | The stored values do not match the source type, for example an entry imported with a missing environment variable. The API does not return the failing fields yet; send a complete `PUT` to fix it, which sets the status back to `active`. |
| `deleting` | A delete is running. |

## Errors

Errors use `{"error": {"code", "message", "details", "trace_id"}}`. A
`422 validation_error` lists each failing field in `details` as
`[{"field": "credentials.base_url", "issue": "..."}]`; fields are named
`credentials.<name>`, `config.<name>` or `advanced.<name>`.

## Removed routes

These are gone in 2.0.0 and answer `404`, with no deprecated alias:

| Route | Replacement |
| --- | --- |
| `GET /v1/connections` | `GET /v1/admin/connections` |
| `/api/v1/sources*` | `/v1/admin/projects/{project_id}/connections/{connection_id}` |
| `POST /v1/ingestions` | `POST /v1/admin/ingestions` |
| `/v1/ingestions/{task_id}*` | `/v1/admin/ingestions/{ingestion_id}*` (`/stream` is `/events`) |

`POST /v1/admin/ingestions` starts a run of the connection its `project_id` and
`connection_id` name. A connection imported from `connectors.yaml` sits in project
`default`, with its YAML key as `connection_id`.
