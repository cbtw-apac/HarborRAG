# Connections

A connection is one configured data source in a project: where and how to
connect (`credentials`) and what to sync (`config`). Connections live in the
control database and are managed through the HTTP API. Every ingestion worker
reads the connection at the start of each run, so a change applies from the
next run on every worker, with no restart.

`config/connectors.yaml` is read once, at the first start of an install that
has no connections, and imported. After that, change connections through the
API. See [Upgrading](upgrading.md).

## Add a connection in three calls

The form an operator fills in maps to three calls. All of them need the
`editor` role.

**1. Pick the data source.** List the source types and their fields:

```bash
curl -H "Authorization: Bearer $TOKEN" https://harborrag.example.com/v1/admin/source-types
```

Each type returns its `auth_methods` (one JSON Schema each), a `config_schema`,
`ui` hints for the form and its `capabilities`. A field with `writeOnly: true`
and `ui.widget: secret` is a secret.

| `source_type` | Category | Auth methods |
| --- | --- | --- |
| `jira` | issue_tracker | `api_token` (Cloud: email + API token), `pat` (Data Center) |
| `confluence` | wiki | `api_token`, `pat` |
| `sharepoint` | files | `app` (client secret), `oauth` (device-code sign-in), `pat` (access token) |
| `local_file` | files | `none` |
| `github` | code | `pat`, `none` (public repository) |

Pick an auth method with `credentials.method`, for example `"method": "pat"`.
Without it, the type's first method in the table applies.

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
`rate_limited`). Up to five sample items show what a sync would read. A test
gives up after 15 seconds. Without a body, the call tests the saved connection.

**3. Save it.** `PUT` the same body; `sync_now` starts the first ingestion:

```bash
curl -X PUT -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  https://harborrag.example.com/v1/admin/projects/cpm/connections/cpm-tickets \
  -d '{"source_type": "jira", "name": "CPM tickets",
       "credentials": {"base_url": "https://acme.atlassian.net",
                       "email": "bot@acme.com", "api_token": "ATATT3x..."},
       "config": {"jql": "project = CPM"},
       "sync_now": true}'
```

`201` means it was created, `200` that it was overwritten. The project must
exist: an admin creates it with `PUT /v1/admin/projects/{project_id}`.

## Change, sync and delete

| Call | What it does |
| --- | --- |
| `GET /v1/admin/connections` | Every connection in the tenant, needing attention (`needs_reauth`, `error`) first. Filter with `project_id`, `source_type`, `status`; page with `limit` and `cursor`. |
| `GET …/connections/{connection_id}` | One connection. Secrets come back as their status, never the value. The `ETag` header is the version. |
| `PUT …/connections/{connection_id}` | Overwrites the connection. `config` is replaced whole. Send `If-Match` with the `ETag` you read to fail with `412` if someone changed it since, or `If-None-Match: *` to create only. `source_type` cannot change (`409 immutable_field`). |
| `POST …/connections/{connection_id}/sync` | Starts an `incremental` or `full` sync and answers `202` with the ingestion. A second start while one runs answers `409 already_running` with the running ingestion's id. |
| `DELETE …/connections/{connection_id}` | Answers `202` with a `job_id`. In the background, the connection's documents leave Qdrant and the graph, then its stored secrets are deleted, then the row. `purge=true` also drops its Postgres history. |
| `GET /v1/projects/{project_id}/connections` | Readers: the project's connections for filter chips. |

A running ingestion keeps the values it started with: a change applies from
the next run.

## Secrets

A secret field accepts a value or a reference:

| You send | What is stored |
| --- | --- |
| `"api_token": "ATATT3x..."` | The value, encrypted in the control database. |
| `"api_token": {"secret_ref": "env:JIRA_TOKEN"}` | A reference. The worker reads `JIRA_TOKEN` from its own environment when the run starts. |
| The field left out | The stored value is kept. Re-saving a form without the key keeps it. |
| `"api_token": null` | The stored value is cleared. |

Reads return a status instead of the value:

```json
"api_token": {"set": true, "last4": "9f2c", "updated_at": "2026-10-28T09:14:03Z", "from": "stored"}
```

`last4` is `null` for references and for values shorter than eight characters.
A replaced stored secret is deleted once no running ingestion still uses it.
Secrets never appear in responses, logs, error details, exports or workflow
history: workflows carry references, and the worker resolves them.

The API and every worker must share the same `HARBORRAG_SECRETS_ENCRYPTION_KEY`.

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

## Connection status

| Status | Meaning |
| --- | --- |
| `active` | The connection works. |
| `needs_reauth` | A run's credentials were rejected. Fix them with `PUT`; the next run that authenticates sets it back to `active`. |
| `error` | The stored values do not match the source type, for example an entry imported with a missing environment variable. `details` in the status lists each field. |
| `deleting` | A delete is running. |

## Errors

Errors use `{"error": {"code", "message", "details", "trace_id"}}`. A
`422 validation_error` lists each failing field in `details` as
`[{"field": "credentials.base_url", "issue": "..."}]`.

## Deprecated routes

These keep working in 2.0.x and send `Deprecation`, `Sunset` and
`Link: rel="successor-version"` headers:

| Route | Successor |
| --- | --- |
| `GET /v1/connections` | `GET /v1/admin/connections` |
| `/api/v1/sources*` | `/v1/admin/projects/{project_id}/connections/{connection_id}` |
| `POST /v1/ingestions` | `POST …/connections/{connection_id}/sync` |

`POST /v1/ingestions` takes the connection's `source_key` as `connection_id`.
For a connection imported from `connectors.yaml`, that is the YAML key.
