# Operate and verify the 2.0.0 upgrade

Use this reference to review what the database migration does, check connections after the upgrade, and plan the first ingestion runs.

Follow the server start order in the [upgrade guide](upgrading.md#upgrade-the-services-in-order).

- [Database migrations and locks](#control-database-migrations)
- [Credential recovery and first ingestion](#after-the-upgrade)
- [One-time import](#what-happens-at-the-first-start) and [old source rows](#rows-from-apiv1sources)
- [Connection changes](#connections) and [known limitations](#known-limitations)

## Control database migrations

The API applies migrations `0041`–`0049` when it starts. Run one API process during and
after the upgrade. A second API process could run the same migrations concurrently.
Migrations run as one transaction that commits at the end. Workers never migrate.
Several migrations lock busy tables until the run commits,
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

## After the upgrade

Once every service runs the new release, fix the connections first: set cleared
credentials and folder settings again, as the first two steps below show. Then start the
`force` re-ingest of every connection that
[Re-index when the embedding model changes](upgrading-2-0-0-configuration.md#re-index-when-the-embedding-model-changes)
asks for; it also covers the SharePoint and Jira steps.

### Set cleared credentials again

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

### Put back spaces in folder and glob settings

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

### Re-ingest SharePoint connections once

SharePoint chunks now carry the document's `created_at`, `updated_at`, `author` and
`web_url`, and its site, drive and item IDs, in their vector payloads, so filters on them
work. A document gets these fields only when it is fetched again, and an incremental run
does not fetch a document that has not changed. Run one ingestion in `force` mode per
SharePoint connection:

```bash
harborrag ingestions start engineering/sharepoint-handbook --mode force --wait
```

The `force` re-ingest of every connection that
[Re-index when the embedding model changes](upgrading-2-0-0-configuration.md#re-index-when-the-embedding-model-changes)
asks for covers this step.

### Run each Jira connection once in full

Jira comments limited to a role or group are no longer indexed, so a reader of the issue
no longer reads them, also under `tenant_shared`. Comments indexed before the upgrade
leave the index on the next full run of each Jira connection: one started without
`updated_after`, from a schedule or by hand. An issue that holds such a comment gets a
new change key, so that run fetches it again without `force` mode:

```bash
harborrag ingestions start engineering/jira-main --wait
```

A re-index alone does not remove them. The versions the run replaces keep the comment
text in their stored artifacts until the retention purge removes them, 30 days after
they are retired by default (`HARBORRAG_RETIRED_VERSION_RETENTION_DAYS`). Comments that
Jira Service Management marks internal, with no role or group, stay indexed.

The `force` re-ingest of every connection that
[Re-index when the embedding model changes](upgrading-2-0-0-configuration.md#re-index-when-the-embedding-model-changes)
asks for covers this step.

### Expect a larger first run

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
[Re-index when the embedding model changes](upgrading-2-0-0-configuration.md#re-index-when-the-embedding-model-changes)
asks for does this work too. From then on, a document fetched again and found unchanged
stores the change key its source reports, so the next run skips it without fetching.

### Paths with unusual spacing scan again

No action is needed. A connection that ingested files whose paths hold a double space, a
tab or a no-break space, or start or end with a space, on 2.0.0a1 or 2.0.0a2, no longer
fails every run. Its first scan moves those source items to the document ID 2.0.0 gives
them.

### Files with decomposed accents are processed once again

No action is needed. 2.0.0 gives a file whose name is not in Unicode NFC, such as
`café.md` with its `é` written as an `e` and a combining accent, its own document ID,
so it no longer shares one with the composed spelling of the same name. Files copied
from macOS often have such names. After the upgrade, each of them is ingested once more
under its new ID. Until a later full run retires the old copy, search can return both,
and that run counts the old copies as removals. Two full runs settle it. Names already
in NFC keep their IDs, and reindex skips a document still stored under its old ID until
the next scan replaces it.

## What happens at the first start

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

## Rows from `/api/v1/sources`

Existing rows become connections. Each gets a `connection_id` slugged from its name,
made unique in its project with `-2`, `-3` and so on, and the `source_key`
`<project_id>/<connection_id>`. A `paused` row becomes `active`; a row whose type is
unknown becomes `error`.

A Jira or Confluence row without an email gets the `pat` method, which a Cloud site
cannot use: starting its ingestion answers `422` on `credentials.email`. `PUT` the
connection with `"method": "api_token"` and the `email`; the stored token is kept.

## Connections

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
  [Set the local source root](upgrading-2-0-0-configuration.md#set-the-local-source-root).
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
- **Jira comments limited to a role or group are left out of the index**, with their
  text, metadata and stored raw payload. See
  [Run each Jira connection once in full](#run-each-jira-connection-once-in-full).
- **A connection test reports a refused request as a permissions failure.** When the
  source answers a `4xx` that is not retried, other than `401`, `403`, `404`, `408` and
  `429`, such as a `400` or `422`, the `permissions` check fails with `error_category`
  `forbidden` and `the source refused the request`. Before, the check that met it failed
  with `unreachable`. Check the connection's settings and the account's permissions
  rather than the network.
- **A local file replaced by a FIFO, a socket or a device fails as unsupported.** After
  discovery, such a path fails as `document_unsupported` and no retry is offered, where
  it failed as a transient error.
- **Removed:** `GET /v1/connections` and `/api/v1/sources*` answer `404`.

## Known limitations

- **Vector and graph indexing still fail together.** A document is published only when
  both its vectors and its graph data are written. While the graph store is down,
  documents fail even when the vector store is healthy, and the cleanup of a failed
  document removes the vectors it had just written. Retry the failures once the graph
  store is back.
- `item_refs` on `POST /v1/admin/ingestions` answers `501 not_supported`.
- Project IDs are unique across tenants.
- **An item with no active version is fetched every run.** When an item's last attempt
  was a permanent rejection, such as a file the parser rejects or one that yields no
  text, it has no active version, so every run fetches and parses it again instead of
  skipping it.
- **A refused source request is still retried in bulk.** `retry-failures` and
  `POST /v1/admin/failures/retry` with a filter select failures by their code, so they
  include a document the source refused, stored as `INVALID_CONFIGURATION` with the
  code `source_unavailable`.
- **Some closed 2.0.0a1 runs cannot be replayed.** Temporal cannot replay a batch
  workflow that 2.0.0a1 recorded and closed after one of its document workflows failed,
  so a query or a reset of such a run, for example from the Temporal UI, fails. A run
  that is still open cannot be in that state.
- **Graph reads for a reader of more than 10,000 documents are narrower.** Under
  `source_acl`, past that budget the graph is filtered by the reader's readable source
  scopes, and each returned document is checked again. In that state:
    - a graph selector must be a node key; a title, logical-ID or provider-ID selector
      is refused with `AUTHORIZATION_UNAVAILABLE`, which the MCP graph tools report as
      `graph retrieval backend failed`, with an error and its traceback in the server
      log;
    - a path search returns no path when every shortest path crosses a document the
      reader may not read, although a longer readable path exists;
    - each graph call lists the reader's documents again before it falls back to source
      scopes.
