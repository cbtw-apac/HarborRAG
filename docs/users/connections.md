# Connections

Connect HarborRAG to a data source, such as a Jira project, a Confluence space, a
SharePoint site, a GitHub repository or a local folder, then start, follow and retry its
ingestions. You do this over the HTTP API, or from a terminal with the `harborrag`
`projects`, `connections`, `ingestions` and `failures` commands, which call the same
operations (see the
[CLI reference](cli-reference/README.md#manage-projects-and-connections)).

## Terms used on this page

- **Tenant**: the isolation boundary that owns data. Everything HarborRAG stores belongs
  to one tenant, and nothing is shared between tenants. A new install has one tenant,
  `DEFAULT`. See [Tenant and workspace status](configuration/workspace-mode.md).
- **Project**: a named group of connections inside a tenant, such as `default` or
  `demo`. A project must exist before you save a connection in it.
- **Connection**: one configured data source in a project. It holds the source type
  (`source_type`), how to sign in (`credentials`), what to read (`config`), how to call
  the source (`advanced`: request rate, paging, timeouts and retries) and a display
  `name`. Its address, the `connection_ref`, is `PROJECT_ID/CONNECTION_ID`, such as
  `demo/demo-tickets`; the CLI names connections by it.
- **`source_key`**: the name that the Python SDK and schedules use for a connection. A
  connection imported from `config/sources/connectors.yaml` keeps its YAML key, such as
  `jira-main`. A connection created through the API or `harborrag connections create`,
  or one that existed before database migration 0041, has the form
  `PROJECT_ID/CONNECTION_ID`.
- **Ingestion**, or run: one pass that reads a connection's source and indexes what
  changed.

Connections are stored in the control database, which also holds projects, settings and
ingestion history. Every ingestion worker reads the connection when a run starts, so a
change applies from the next run on every worker, with no restart.

## Where the first connections come from

`config/sources/connectors.yaml` is read once, when the API first starts on an install
that holds no project and no connection yet. Its entries become connections in project
`default` of the tenant that `HARBORRAG_TENANT_ID` names (`DEFAULT` unless set). Each
YAML key becomes the connection's `connection_id` and its `source_key`, so the entry
`jira-main` becomes the connection `default/jira-main`. The attempt is recorded, a
failed one too, and the file is never read again. After that, change connections
through the API or the `harborrag connections` commands. The file format is in
[Connector configuration](configuration/connector-config.md); moving from 1.x is in
[Upgrading](upgrading.md).

## Before you start

- The API's address. On the local stack it is `http://127.0.0.1:8000`; the examples
  below use `https://harborrag.example.com`.
- A bearer token in `$TOKEN` whose role is `editor` or higher. Roles and tokens are
  described in
  [Configure API authentication](configuration/workspace-mode.md#configure-api-authentication).
  On the local stack, `env/.env.api` (copied from `env-example/.env.api.example`) sets
  `HARBORRAG_AUTH_MODE=none`, which treats every request from the same machine as
  `owner`, so leave out the `Authorization` header there.
- A project. Creating one needs the `admin` role:

    ```bash
    curl -X PUT -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
      https://harborrag.example.com/v1/admin/projects/demo \
      -d '{"project_name": "Demo"}'
    ```

    `201` means the project was created, `200` that it existed and was updated. From a
    terminal, `harborrag projects create demo --project-name Demo` creates it and fails
    with `412` when it exists.

## Add a connection

Adding a connection takes four steps: pick the source type, test the values, save them,
and start the first ingestion. Each step needs the `editor` role. To fill the request
body in a browser instead, see [Swagger UI templates](#swagger-ui-templates).

### 1. Pick the source type

List the source types and the fields each one takes:

```bash
curl -H "Authorization: Bearer $TOKEN" https://harborrag.example.com/v1/admin/source-types
```

Each type returns its `auth_methods` (one JSON Schema and one set of `ui` form hints
each), a `config_schema`, an `advanced_schema`, `ui` hints for the config and advanced
fields, and its `capabilities`. A field with `writeOnly: true` and `ui.widget: secret`
is a secret. When a type has several auth methods, each method's schema has a `method`
property whose `const` is the value to send. The config and advanced schemas publish
each field's `default`: the value the connector uses when the field is left out.
`ui.widget` is `text`, `secret`, `select`, `textarea`, `path`, `list`, `toggle` or
`number`.

| `source_type` | Category | Setup | `credentials.method` |
| --- | --- | --- | --- |
| `jira` | `issue_tracker` | Jira Cloud: email and API token | `api_token` (default) |
| | | Jira Data Center: personal access token | `pat` |
| `confluence` | `wiki` | Confluence Cloud: email and API token | `api_token` (default) |
| | | Confluence Data Center: personal access token | `pat` |
| `sharepoint` | `files` | App registration with a client secret | `app` (default) |
| | | Device-code sign-in as a user | `oauth` |
| | | Microsoft Graph access token, for a one-off test | `pat` |
| `github` | `code` | Repository read with an access token | `pat` (default) |
| | | Public repository, no token | `none` |
| `local_file` | `files` | A folder the workers can read | `none` (default) |

Send `credentials.method` to pick a method, for example `"method": "pat"`. When you
leave it out, the method marked default applies.

URL fields that carry credentials (`base_url`, `api_url`, `web_url`, `graph_api_url`,
`site_url`) must be `https://`. The schemas state each type's rules with `pattern`,
`anyOf` and `allOf`, and a `PUT` or `test` that breaks them, or any other rule the
connector checks, answers `422` naming the field.

<details markdown="1">
<summary>Jira and Confluence: Cloud or Data Center</summary>

- **Cloud** (`api_token`): set `base_url` to your site, such as
  `https://your-company.atlassian.net`, `email` to the account the token belongs to, and
  `api_token` to the token. Create the token as described in Atlassian's
  [Manage API tokens for your Atlassian account](https://support.atlassian.com/atlassian-account/docs/manage-api-tokens-for-your-atlassian-account/).
- **Data Center** (`pat`): set `base_url` to your server and put the personal access
  token in `api_token`. Create the token as described in Atlassian's
  [Using Personal Access Tokens](https://confluence.atlassian.com/enterprise/using-personal-access-tokens-1026032365.html).
  A server inside your network also needs [Network access](#network-access).
- **What to read**: Jira reads the issues that `jql` or the `project_keys`,
  `issue_types`, `statuses` and `labels` filters select. Confluence reads one
  `space_key` and the `content_types` you list (`page`, `blogpost`). Every field is in
  the type's `config_schema` and in
  [Connector configuration](configuration/connector-config.md).

</details>

<details markdown="1">
<summary>SharePoint: app registration, device-code sign-in or access token</summary>

SharePoint is read through Microsoft Graph with a Microsoft Entra app registration. In
these fields, `tenant_id` is your Microsoft Entra tenant, not a HarborRAG tenant.

- **App registration** (`app`): set `tenant_id`, `client_id` and `client_secret`.
  Register the app as described in Microsoft's
  [Register an app in Microsoft Entra ID](https://learn.microsoft.com/en-us/entra/identity-platform/quickstart-register-app)
  and add a client secret as described in
  [Add and manage app credentials](https://learn.microsoft.com/en-us/entra/identity-platform/how-to-add-credentials).
  The Graph permission the connector accepts is in
  [Connector configuration: Permissions](configuration/connector-config.md#permissions).
- **Device-code sign-in** (`oauth`): set `tenant_id` and `client_id`; no client secret
  is used. The connector reads as a user who signed in once, with Microsoft's
  [device authorization grant](https://learn.microsoft.com/en-us/entra/identity-platform/v2-oauth2-device-code).
  The app registration's settings are in
  [Connector configuration: Delegated sign-in](configuration/connector-config.md#delegated-sign-in).
- **Access token** (`pat`): set `access_token` to a Microsoft Graph access token. It
  expires within about an hour, so it suits a one-off test rather than syncs.
- **What to read**: one site, named by `site_url`, `site_id`, or `hostname` and
  `site_path`.

The API cannot show the device-code sign-in prompt, so sign in from a terminal. On the
local stack, put the connection's IDs in `env/.env.connector` as `MICROSOFT_TENANT_ID`
and `MICROSOFT_CLIENT_ID`, then run:

```bash
scripts/deployment/dev.sh sharepoint-login
```

It signs in inside the worker image and keeps the sign-in in the worker's
`harborrag-sharepoint-tokens` volume. Elsewhere, run the sign-in on a host whose
`token_cache_path` the workers can also read:

```bash
python -m harborrag_adapters.connectors.sharepoint.login \
  --tenant-id <entra-tenant-id> --client-id <client-id> --harborrag-tenant DEFAULT
```

Without a `token_cache_path`, the sign-in is kept per HarborRAG tenant, so pass the
connection's tenant to `--harborrag-tenant` (the default is `HARBORRAG_TENANT_ID`, else
`DEFAULT`). `token_cache_path` is a path on the worker, so only
`config/sources/connectors.yaml` and the `harborrag connections` commands can set or
change it;
through the API, send it back as read, or leave it out when it was never set.

</details>

<details markdown="1">
<summary>GitHub: access token or public repository</summary>

- GitHub is read through its REST API over HTTPS, so it signs in with a token, not an
  SSH key. Create one as described in GitHub's
  [Managing your personal access tokens](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/managing-your-personal-access-tokens)
  and set it as `token` (`pat`).
- A public repository can be read without a token (`none`), but GitHub then allows 60
  requests an hour, which a large repository exceeds (see GitHub's
  [Rate limits for the REST API](https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api)).
- For GitHub Enterprise Server, set `credentials.api_url` and `credentials.web_url`.
- **What to read**: `repository_url`, or `owner` and `repo`. An SSH URL such as
  `git@github.com:acme/handbook.git` is accepted as `repository_url`; only its owner and
  name are used. Pin a revision with `ref` or `branch`, or with `commit_sha`, not both.
  The default file types leave out source code; see
  [File type filters](#file-type-filters).

</details>

A `local_file` connection reads a folder on the worker; see [Local files](#local-files).

### 2. Test the values

Send the form values to the `test` route. Nothing is saved:

```bash
curl -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  https://harborrag.example.com/v1/admin/projects/demo/connections/demo-tickets/test \
  -d '{"source_type": "jira",
       "credentials": {"base_url": "https://acme.atlassian.net",
                       "email": "bot@acme.com", "api_token": "ATATT3x..."},
       "config": {"jql": "project = DEMO"}}'
```

The answer is `200` for any valid input. When the values work, `ok` is `true`, each
entry of `checks` (`schema`, `auth`, `reachable`, `permissions`, `preview`) has
`"ok": true`, and `preview.sample` lists up to five items a sync would read. When a
check fails, `ok` is `false`, the failing check carries a `message`, and
`error_category` says why: `auth_invalid`, `auth_expired`, `forbidden`, `unreachable`,
`rate_limited`, or `secret_unavailable` when a token is an `env:` reference that only
the worker can read. A test gives up after 15 seconds. Without a body, the call tests
the saved connection.

From a terminal, `harborrag connections test demo/demo-tickets -f demo-tickets.yaml`
tests a body file the same way. It exits 0 even when a check fails, as the route answers
`200`, so read `ok` from its output.

### 3. Save the connection

`PUT` the same body, with a display `name`:

```bash
curl -X PUT -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  https://harborrag.example.com/v1/admin/projects/demo/connections/demo-tickets \
  -d '{"source_type": "jira", "name": "Demo tickets",
       "credentials": {"base_url": "https://acme.atlassian.net",
                       "email": "bot@acme.com", "api_token": "ATATT3x..."},
       "config": {"jql": "project = DEMO"}}'
```

`201` means it was created, `200` that it was overwritten. There is no separate create
call: a `PUT` to a new `connection_id` creates it. Use the project's ID, such as
`default`, not its display name; a write that names a project by its display name
answers `404 project 'Default' not found; did you mean 'default'?`.

From a terminal, `harborrag connections create demo/demo-tickets -f demo-tickets.yaml`
creates the connection from a YAML or JSON body file and refuses an ID that already
exists with `412`. Leave the token out of the file and add `--secret-prompt api_token` to
type it without echo.

### 4. Start the first ingestion

Saving a connection never starts an ingestion. Start one:

```bash
curl -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  https://harborrag.example.com/v1/admin/ingestions \
  -d '{"project_id": "demo", "connection_id": "demo-tickets"}'
```

The answer is `202` with the new run, including its `ingestion_id` and `status`. Follow
it with `GET /v1/admin/ingestions/{ingestion_id}` until `status` is `succeeded`, or
stream its events; see [Start and control ingestions](#start-and-control-ingestions).
From a terminal, `harborrag ingestions start demo/demo-tickets` starts the run and prints
it; add `--wait` to follow it until it ends.

## Swagger UI templates

The API's interactive documentation, at `/docs`, offers a complete request body for each
setup in the table above. Open **Create or replace a connection** (or
**Test a connection**), select **Try it out**, and pick a template from the list Swagger
UI labels **Examples**:

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

Each template sets `credentials.method` and lists every `config` and `advanced` field
with the value a sync uses when you leave it as is: the connector's own defaults, such
as `max_comments: 1000` for Jira or the default file types for SharePoint. Replace the
placeholder values, such as `bot@acme.com` and `ATATT3x…`, then run it. `project_id` is
filled with `default`; type a new `connection_id` to create a connection.

To edit a saved connection, read it with **Get a connection** and copy its
`credentials`, `config` and `advanced` into the `PUT` body with `source_type` and
`name`. The other fields of the response, such as `status` and `created_at`, are
rejected in a request. A secret reads back as its status; leave it out, or paste the
status back, to keep the stored value.

## Read, change and delete connections

These routes need the `editor` role, except the reader list.

| Call | What it does |
| --- | --- |
| `GET /v1/admin/connections` | Every connection in the tenant, those needing attention (`needs_reauth`, `error`) first. Filter with `project_id`, `source_type`, `status`; page with `limit` and `cursor`. |
| `GET /v1/admin/projects/{project_id}/connections/{connection_id}` | One connection. Secrets come back as their status, never the value. The `ETag` header is the version. |
| `PUT /v1/admin/projects/{project_id}/connections/{connection_id}` | Creates or overwrites the connection. `config` is replaced whole, and so is `advanced` unless it is left out. Send `If-Match` with the `ETag` you read to fail with `412` if someone changed it since, `If-Match: *` to update only, or `If-None-Match: *` to create only. `source_type` cannot change (`409 immutable_field`, `details` `{"field": "source_type"}`). A `PUT` that changes nothing stores nothing: the `ETag` and `updated_at` stay the same. Saving never starts an ingestion. |
| `DELETE /v1/admin/projects/{project_id}/connections/{connection_id}` | Answers `202` with a `job_id`. In the background, the connection's documents leave Qdrant and the graph, then its stored secrets are deleted, then the row. `purge=true` also drops its Postgres history. |
| `GET /v1/projects/{project_id}/connections` | Role `reader`: the project's connections, for filter chips. |

## Start and control ingestions

These routes need the `editor` role. A run reads the connection as it was when the run
started; a change applies from the next run. A connection imported from
`connectors.yaml` sits in project `default`, with its YAML key as `connection_id`.

### `POST /v1/admin/ingestions`

Starts an ingestion of the connection that `project_id` and `connection_id` in the body
name, and answers `202` with the run and its `Location`.

| Body field | Meaning |
| --- | --- |
| `project_id`, `connection_id` | The connection to ingest. Required. |
| `mode` | `incremental` (default) or `force`; see [Ingestion modes](ingestion-modes.md). |
| `updated_after` | Re-read items changed since this date-time, for example to backfill a window. It needs a UTC offset, such as `2026-11-01T00:00:00Z`. |
| `item_refs` | Not supported yet: a non-empty list answers `501 not_supported`. |

A second start while one runs answers `409 already_running`, with `details.ingestion_id`
naming the running ingestion. An unknown project or connection is
`422 reference_not_found`. A connection whose stored values fail its source type's
schema is `422 validation_error` with each failing field, and nothing starts.

### `GET /v1/admin/ingestions`

Runs in the tenant, newest first. Filter with `project_id`, `connection_ref`
(`project_id/connection_id`), `status` (`queued`, `running`, `paused`, `succeeded`,
`failed`, `cancelled`) and `created_after` (a date-time with a UTC offset, such as
`2026-11-01T00:00:00Z`; without one it is `422`). Page with `limit` (default 20, 1 to
100) and `cursor`, which is bound to the filter it came from. A run in another tenant is
`404`.

### `GET /v1/admin/ingestions/{ingestion_id}`

One run: `ingestion_id`, `tenant_id`, `project_id`, `connection_id`, `mode`, `status`,
`counters` (`discovered`, `processed`, `skipped`, `failed`), `stages` (empty),
`error_category`, `triggered_by`, `settings_revision`, `workflow_id`, `run_id`,
`trace_id`, `links` (`temporal`, `grafana`, `langfuse`), `created_at`, `finished_at`.

### `POST /v1/admin/ingestions/{ingestion_id}/pause`, `/resume`, `/cancel`

These answer `200` with the run after waiting for the new status: up to 10 s for pause
and resume, 30 s for cancel. If the wait ends first, the answer is the run as it stands.

- `pause` is allowed from `queued` or `running`, `resume` from `paused`, and `cancel`
  from `queued`, `running` or `paused`. Any other state is `409 invalid_state`, and the
  run is unchanged.
- A retry run, started by `retry-failures` or `POST /v1/admin/failures/retry`, can be
  cancelled but not paused or resumed: `pause` and `resume` on it are
  `409 invalid_state`.
- Cancelling a run whose Temporal workflow was already terminated records it as
  `cancelled`. So does cancelling a running or paused run, or a retry run, whose
  workflow Temporal no longer has, for example after its history passed the namespace
  retention; its `error_category` is then `workflow_not_found`.
- Cancelling a queued run that is not a retry run, and whose workflow Temporal does not
  have, answers `503`, since the workflow may not have started yet.

### `POST /v1/admin/ingestions/{ingestion_id}/retry-failures`

Starts a new run for the documents that failed in a `succeeded`, `failed` or `cancelled`
run with `counters.failed` above 0 (otherwise `409 invalid_state`). Answers `202` with
the new run and its `Location`.

### `GET /v1/admin/ingestions/{ingestion_id}/events`

Server-Sent Events of one run: a replay of what is stored, then a live tail that ends
after `run.completed`.

- The response opens at once with `200`, and a `: ping` comment arrives every 15 s while
  nothing else does, even before the first event.
- A stream still open after `api_stream_timeout_seconds` (600 s by default) ends with an
  `error` event whose `code` is `deadline_exceeded`.
- A run that ended gets its `run.completed` when a watcher reads it, once, even if no
  event was sent live. Reconnecting to an ended run with nothing stored after
  `Last-Event-ID` answers `204`, so a browser `EventSource` stops reconnecting.
- Each frame has an integer `id` that only increases, and a reconnect with
  `Last-Event-ID` resumes after that ID.
- `run.status` is `{"status", "counters"}` (the run's status and counters), sent when
  either changes, so a pause or resume shows as the new status. It covers `queued`,
  `running` and `paused`; the terminal state arrives only in `run.completed`.
- `stage.progress` is `{"stage", "done", "total"}` and counts the documents that left
  the pipeline (`processed` + `skipped` + `failed`) against `counters.discovered`, under
  stage `publish`.
- `document.failed` is `{"document_id", "stage", "error_category"}` for each failed
  document, with the document's source reference when it has one and the `stage` and
  `error_category` of the documents page.
- `run.completed` is `{"counters", "finish_reason"}`, where `finish_reason` is the final
  status (`succeeded`, `failed` or `cancelled`), so a cancel arrives here.

A run in another tenant is `404`.

### `GET /v1/admin/ingestions/{ingestion_id}/documents`

One item per document: `document_ref`, `document_id`, `status` (`succeeded`, `skipped`
or `failed`), `stage` (`capture`, `interpret`, `commit`, `derive` or `publish`),
`error_category` and `duration_ms` (`null` for now). Filter with `status=all` (default),
`succeeded`, `skipped` or `failed`; page with `limit` (default 20, 1 to 100) and
`cursor`.

## Review and retry failures

These routes need the `editor` role.

### `GET /v1/admin/failures`

The failure inbox: each document's latest failed result in the tenant. Filter with
`project_id`, `connection_ref`, `error_category` (`unknown` matches failures without a
code), `stage` (`capture` also matches stages with no mapping), `q` and `resolved`
(default `false`; a document is resolved once a later result for it succeeded, was
unchanged, skipped or removed). The response carries `counts` per `error_category` over
the whole filter; `total` is present only with `include_total=true`. Page with `limit`
and `cursor`.

### `POST /v1/admin/failures/retry`

Retries unresolved failures. The body has exactly one of `ids` (failure IDs) or `filter`
(`connection_ref`, `error_category`, `stage`), else `422 validation_error`. It starts
one retry run per original run and answers `202` with
`{"ingestion_id", "ingestion_ids", "queued"}`: the first new run, every new run, and the
number of documents queued. Runs that cannot be retried are skipped, and only started
runs are reported. When no unresolved, retryable failure matches, no run starts and the
answer is `409 invalid_state`, so a `202` always means at least one run started.

## Secrets

A secret field accepts a value or a reference:

| You send | What is stored |
| --- | --- |
| `"api_token": "ATATT3x..."` | The value, encrypted in the control database. Sending the value already stored keeps it, and clears a `needs_reauth` status. |
| `"api_token": {"secret_ref": "env:JIRA_TOKEN"}` | A reference: the worker reads `JIRA_TOKEN` from its own environment when the run starts. Only `config/sources/connectors.yaml` and the `harborrag connections` commands can set one; the API answers `422` and asks for the value. |
| The field left out, or its status sent back | The stored value or reference is kept. Re-saving a form without the key keeps it. |
| `"api_token": null` | Refused with `422` and `required`; the stored value is kept. Every secret is required by its auth method, so switch `method` to drop it. |

A stored secret is kept only for the same auth method and the same endpoint. Switching
`method` drops the old method's secrets, and they are deleted like a replaced secret.
Changing `base_url`, `api_url`, `web_url` or `graph_api_url` without sending the secret
again answers `422` with `send it again: base_url changed`, so a stored secret is never
sent to a new host.

Reads return a status instead of the value:

```json
"api_token": {"set": true, "last4": "9f2c", "updated_at": "2026-10-28T09:14:03Z", "from": "stored"}
```

`last4` is `null` for references and for values shorter than eight characters. A
replaced stored secret is deleted once no running ingestion still uses it. Secrets never
appear in responses, logs, error details, exports or workflow history: workflows carry
references, and the worker resolves them.

Stored values are encrypted with `HARBORRAG_SECRETS_ENCRYPTION_KEY`, which the API and
every worker must share. Back the key up: without it, stored secrets cannot be read and
must be entered again.

- On the local stack, `scripts/deployment/dev.sh` writes a new key to
  `env/.env.database` when the key is blank and no database volume exists yet. When the
  key is blank but the volume already holds a database, `dev.sh` stops with
  `HARBORRAG_SECRETS_ENCRYPTION_KEY is blank in <file>, but <volume> already holds a database; restore the key it was written with.`
- To set a key by hand, generate one with `openssl rand -hex 32`.
- In production, mount the key as a file, such as a Docker or Kubernetes secret, and set
  `HARBORRAG_SECRETS_ENCRYPTION_KEY_FILE` to its path.

## Network access

Connectors, and connection tests run by the API, reach public addresses only. A
connection whose host resolves to a private, loopback, link-local or reserved address,
such as `10.0.0.5`, `172.18.0.1`, `127.0.0.1` or the cloud metadata address
`169.254.169.254`, fails its `reachable` check with a message naming the address. The
check runs on the address each connection actually reaches, so a DNS answer that changes
later is caught too.

To reach a host inside your network, such as Jira Data Center, list its host name or
network in `HARBORRAG_CONNECTOR_ALLOWED_HOSTS`, in both `env/.env.connector` (workers)
and `env/.env.api` (tests):

```dotenv
HARBORRAG_CONNECTOR_ALLOWED_HOSTS=jira.acme.internal,10.20.0.0/16
```

Connectors ignore `HTTP_PROXY` and `HTTPS_PROXY` and connect directly, so a proxy
variable never switches these checks off. To send connector traffic through a proxy,
also set `HARBORRAG_CONNECTOR_USE_PROXY=true` in both env files. HarborRAG then checks
the target host's own addresses before handing the request to the proxy, which decides
where it finally goes.

## Advanced settings

`advanced` controls how HarborRAG calls the source. Its fields come from the type's
`advanced_schema`:

| Field | Types | Connector default |
| --- | --- | --- |
| `requests_per_minute` | Jira, Confluence, SharePoint, GitHub | 60 (Jira, Confluence), 120 (SharePoint, GitHub) |
| `page_size` | Jira, Confluence, SharePoint | 50 (Jira), 25 (Confluence), 200 (SharePoint) |
| `request_timeout_seconds` | Jira, Confluence, SharePoint, GitHub | 30, 60 for SharePoint |
| `max_retries` | Jira, Confluence, SharePoint, GitHub | 3 |
| `backoff_factor` | Jira, Confluence, SharePoint, GitHub | 0.5 |

Local files have no advanced settings. `advanced` stays on the connection; the settings
store holds only the keys of [Runtime settings](configuration/runtime-settings.md).

| You send | Result |
| --- | --- |
| `advanced` left out | The stored values are kept. |
| `"advanced": {"page_size": 20}` | Replaces the stored values; other fields use the connector default. |
| `"advanced": {}` | Every field uses the connector default. |
| `"advanced": {"page_size": null}` | That field uses the connector default. |

Jira and Confluence accept a `page_size` from 1 to 100. Settings imported from
`connectors.yaml`, such as `requests_per_minute: 600`, show up here.

## File type filters

`config.allowed_extensions` keeps only the listed file types, and
`config.excluded_extensions` drops the listed ones. Write them with or without the dot
and in any case: `pdf`, `.PDF`. An excluded type wins over an allowed one.

When `allowed_extensions` is left out, every source type keeps the same document and
image types: `md`, `markdown`, `txt`, `pdf`, `docx`, `doc`, `epub`, `odt`, `pptx`,
`csv`, `xlsx`, `png`, `jpg`, `jpeg` and `html`. An explicit empty list,
`"allowed_extensions": []`, keeps every type, except for SharePoint, which needs at
least one.

| `source_type` | What the filters apply to |
| --- | --- |
| `jira`, `confluence` | Attachments only; issues and pages are always read |
| `sharepoint` | Files in the document libraries |
| `local_file`, `github` | Files |

The default list leaves out source code, so a GitHub connection that should read `.py`
or `.yaml` files lists them, or sends `[]`.

For Jira and Confluence, the type comes from the attachment's file name, or from its
media type when the name has none, so `allowed_extensions: [pdf]` keeps a PDF called
`report`. An attachment the filter drops is not downloaded.

```json
"config": {"project_keys": ["DEMO"], "include_attachments": true,
           "allowed_extensions": ["pdf", "docx"], "excluded_extensions": ["exe"]}
```

Changing a filter keeps the connection's source scope. The next full sync removes
documents for files the filter now drops; an incremental sync leaves them in place.

## Document limits

Each connection skips a file or attachment larger than its size limit, before
downloading it. When the connection does not set one, the limit is 20 MiB (20,971,520
bytes):

| `source_type` | Field |
| --- | --- |
| `jira`, `confluence` | `config.max_attachment_size_bytes` |
| `sharepoint`, `local_file`, `github` | `config.max_file_size_bytes` |

A connection can set a different size, and connections imported with a larger size keep
it. GitHub never reads a file above its 100 MB blob limit.

The parsers then refuse a document that is too large to read, whatever the connection
says. The document is reported as failed and is not retried:

| Document | Limit |
| --- | --- |
| PDF | 200 pages |
| Image | 25 megapixels |
| Spreadsheet (`xlsx`, `xls`, `csv`) | 50,000 rows or 200,000 cells per file |
| Office and EPUB archives (`docx`, `pptx`, `xlsx`, `odt`, `epub`) | 256 MiB unzipped, 5,000 members, 100:1 compression per member |

The PDF and image limits are set in `config/ingestion/parsers.yaml`
([Parser configuration](configuration/parser-config.md)).

## Tenants

The tenant comes from the credential. A credential that holds one tenant acts in it. A
credential that holds several sends `X-Tenant-Id` to pick one; without the header the
call fails with `400 tenant_required`. A wildcard (`*`) credential without the header
acts in the tenant `HARBORRAG_TENANT_ID` names (`DEFAULT` unless set). A tenant the
credential does not hold gives `403 tenant_forbidden`. The CLI takes the tenant from
`--tenant`.
See [Tenant and workspace status](configuration/workspace-mode.md).

## Local files

A `local_file` connection reads a path on the worker. When `HARBORRAG_LOCAL_SOURCE_ROOT`
is set, `source_path` must be an absolute path inside that directory, and a symbolic
link out of it is refused too.

- Only `HARBORRAG_LOCAL_SOURCE_ROOT` sets the root. `LOCAL_SOURCE_PATH` does not.
- Set the same value on the API, the workers and the `harborrag` CLI. The compose files
  set it for the API and the workers. From a checkout, the CLI reads it from
  `env/.env.database`.
- Without a root, the API refuses `local_file` connections with `422` on
  `config.source_path`, since any file the workers can read would otherwise be accepted.
  `config/sources/connectors.yaml` and the `harborrag connections` commands can still add
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
`credentials.<name>`, `config.<name>` or `advanced.<name>`. The CLI prints the same
envelope on stderr with `-o json`, and exits with the code its
[status maps to](cli-reference/README.md#read-errors-and-exit-codes).

## Removed routes

These are gone in 2.0.0 and answer `404`, with no deprecated alias:

| Route | Replacement |
| --- | --- |
| `GET /v1/connections` | `GET /v1/admin/connections` |
| `/api/v1/sources*` | `/v1/admin/projects/{project_id}/connections/{connection_id}` |
| `POST /v1/ingestions` | `POST /v1/admin/ingestions` |
| `/v1/ingestions/{task_id}*` | `/v1/admin/ingestions/{ingestion_id}*` (`/stream` is `/events`; its `task.<id>.progress`, `.done` and `.control` events are now `run.status`, `stage.progress`, `document.failed` and `run.completed`) |
