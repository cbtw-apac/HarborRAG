# Upgrading

## From 2.0.0a2 to 2.0.0

Connections move from `config/connectors.yaml` into the control database and
are managed through the API ([Connections](connections.md)). Ingestion reads
each connection at the start of every run.

### Before you start

- Workers now read connections, revisions and stored secrets from the control
  database. Give every worker the same `HARBORRAG_CONTROL_DB_URL` and
  `HARBORRAG_SECRETS_ENCRYPTION_KEY` as the API. A worker refuses to start
  with a blank key against a non-SQLite database.
- The API applies migrations `0041`–`0043` at start-up. Start the API before
  the workers.
- Give the API the environment variables `config/connectors.yaml` names, for
  example `JIRA_BASE_URL` or `LOCAL_SOURCE_PATH`. The one-time import reads
  non-secret values from the API's environment.

### What happens at the first start

The API imports `config/connectors.yaml` once, when all of these hold:

- the file exists;
- the install has at most one tenant;
- no connection was created by a person (rows carried over from
  `/api/v1/sources` do not count);
- no earlier import was recorded.

Each entry becomes a connection:

- The entry's key becomes the connection's `source_key`, so its documents keep
  their ids and source scopes. The key is also its `connection_id` when it is a
  valid id (lowercase letters, digits, `-` and `_`); otherwise a slug of it is.
- `settings` and `environment` values are split into credentials and config.
  Environment values are read at import time.
- `secrets: <field>_env: NAME` becomes the reference
  `{"secret_ref": "env:NAME"}`. The value stays in the worker's environment.
- Tuning such as `requests_per_minute` and `page_size` is kept on the
  connection and shown as its `advanced` settings.
- An entry with `project_id` goes to that project; others go to `default`,
  created when missing.
- A disabled entry is skipped. An entry with a missing environment variable or
  an invalid value is imported with status `error` and its field issues; fix it
  with `PUT`.

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

### Behaviour that changes

- **Configuration fingerprints change once.** Runs now check a fingerprint of
  the stored connection. Retrying the failures of a run started before the
  upgrade reports a configuration difference; start a new sync instead.
- **A change applies from the next run, on every worker, without a restart.**
  A run that is already going keeps the values it started with.
- **Editing a Jira connection's `jql` keeps its source scope**, so items the
  new query no longer matches are removed by the next full scan.
- **`GET /v1/connections` lists stored connections** in the caller's tenants.
  Its `source_type` for local files is now `local_file`, and `connection_id`
  is the `source_key` that `POST /v1/ingestions` accepts.
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
- **Local paths can be restricted.** With `HARBORRAG_LOCAL_SOURCE_ROOT` or
  `LOCAL_SOURCE_PATH` set, a `local_file` connection must stay inside it.
- **Deprecated:** `GET /v1/connections`, `/api/v1/sources*` and
  `POST /v1/ingestions` keep working through 2.0.x and announce their
  successors. They sunset on 7 February 2027.

### Not yet supported

- `item_refs` on `POST …/sync` answers `501 not_supported`.
- `GET /v1/admin/ingestions/{ingestion_id}`, which the sync response's
  `Location` header names, arrives with the ingestion routes; until then use
  `GET /v1/ingestions/{task_id}`.
- Project ids are unique across tenants.
