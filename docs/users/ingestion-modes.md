# Ingestion modes

Choose whether an ingestion skips the records it already indexed (`incremental`) or
fetches and evaluates every record again (`force`). An ingestion is one run that reads a
connection's source; a connection is one configured data source, such as a Jira project
or a local folder. Both terms are defined in [Connections](connections.md#terms-used-on-this-page).
The mode never changes what a connection reads: paths, queries, credentials, attachments
and comments stay in the connection's stored configuration.

## Set the mode where you start a run

Every way of starting a run sets the same switch:

| Where you start the run | How to force re-evaluation |
| --- | --- |
| HTTP API | `"mode": "force"` in the `POST /v1/admin/ingestions` body |
| CLI | `harborrag ingestions start PROJECT_ID/CONNECTION_ID --mode force`, with or without `--inline` |
| Python SDK | `IngestionRequest(force_reprocess=True)` |
| Schedule | `mode: force` under `source:` in `config/sources/schedules.yaml`; see [Scheduled ingestion](scheduled-ingestion.md) |

Without one of these, a run is `incremental`. `PROJECT_ID/CONNECTION_ID` is the
connection's ref, such as `default/harborrag-workspace`; see
[Run and control ingestion](cli-reference/README.md#run-and-control-ingestion).

The mode is not `execution_mode`, which is `direct` or `temporal` and chooses how a run
executes rather than what it admits.

| Mode | Behavior | Use it for |
| --- | --- | --- |
| `incremental` | Discovers the connection's source scope and skips records whose source, processing profile, canonical content and retrieval metadata are unchanged. | Routine scheduled and manual ingestion. This is the default. |
| `force` | Discovers the same scope, then fetches, parses and evaluates records even when their source descriptors look unchanged. | Recovering from unreliable source change metadata, checking a connector or parser fix, or deliberately evaluating every discovered record again. |

## Run an incremental ingestion

Start an incremental ingestion of the shipped `harborrag-workspace` connection, which
reads the local folder that `LOCAL_SOURCE_PATH` names in `env/.env.connector`:

```bash
curl --fail-with-body \
  --request POST \
  --header 'Content-Type: application/json' \
  --data '{
    "project_id": "default",
    "connection_id": "harborrag-workspace",
    "mode": "incremental"
  }' \
  http://127.0.0.1:8000/v1/admin/ingestions
```

The local stack's API needs no token from the same machine; anywhere else, add
`--header "Authorization: Bearer $TOKEN"` (see
[Connections: Before you start](connections.md#before-you-start)). The answer is `202`
with the new run's `ingestion_id`; follow it as described in
[Start and control ingestions](connections.md#start-and-control-ingestions). Because
`incremental` is the default, leaving out `mode` has the same effect.

Incremental mode is change-aware, but it is not a timestamp-only delta query. HarborRAG
scans the source scope that the connection's configuration and query define, records an
authoritative source scan, and compares each record with the active document version:

- New source record: process and publish it.
- Changed source or canonical content: create and publish a new immutable version.
- Changed retrieval metadata: update the version and rebuild the required projections.
- Changed processing fingerprint: reprocess it, even in incremental mode.
- Unchanged record: keep the active version and skip the expensive downstream work.
- Missing record: apply the consecutive-miss policy before retiring it.

So `incremental` still detects removals, and it works with connectors whose change
tokens are not fully reliable.

There are two change checkpoints. Descriptor admission runs before content is loaded and
can skip a source item cheaply. Canonical admission runs after parsing and normalization,
and prevents a new version when the resulting content, retrieval metadata and processing
fingerprint are identical. See the [data lifecycle](../developers/architecture/data-lifecycle.md)
for the full artifact, projection, verification and publication path.

### Narrow a run

A run reads the scope the connection's configuration defines. To re-read only the items
changed since a date-time, send `updated_after` in the HTTP body, or pass
`--updated-after` to `harborrag ingestions start`, with a UTC offset such as
`2026-11-01T00:00:00Z`; see
[`POST /v1/admin/ingestions`](connections.md#post-v1adminingestions). To narrow what a
connection reads, change its `config`.

## Run a force ingestion

Start a force ingestion of the same connection:

```bash
curl --fail-with-body \
  --request POST \
  --header 'Content-Type: application/json' \
  --data '{
    "project_id": "default",
    "connection_id": "harborrag-workspace",
    "mode": "force"
  }' \
  http://127.0.0.1:8000/v1/admin/ingestions
```

Force mode bypasses the early unchanged check. Every discovered record is fetched and
normalized again, and HarborRAG then computes its deterministic document-version
identity.

Force mode is idempotent. If the resulting content, retrieval metadata and processing
fingerprint match the active version, that version stays active and its completed
projections are not rewritten. If any of them changed, the normal version, projection,
verification and publication pipeline runs.

Force mode does not:

- delete documents, Qdrant collections, graph data or canonical artifacts;
- create a new version when the deterministic version inputs are identical;
- rebuild already-active projections only because their storage collection changed;
- override paths, scopes, credentials, attachments, comments or other connection
  settings;
- retry only the documents that failed in an earlier run.

To retry the failed documents of one run, use its
[`retry-failures` route](connections.md#post-v1adminingestionsingestion_idretry-failures).
To rebuild vector or graph projections when canonical documents are unchanged, for
example after changing Qdrant collection names, the connector-free `harborrag.reindex`
workflow is the tool, but no CLI command or HTTP route starts it yet; see
[Rebuild projections](../developers/architecture/projection-rebuild.md#rebuild-projections).

## Run the CLI from a checkout

From a repository checkout, `harborrag ingestions start` refuses the local SQLite
control database until `HARBORRAG_CONTROL_DB_URL` points at the stack's Postgres. The
setup is in [Run from a repository checkout](cli-reference/README.md#run-from-a-repository-checkout).
Three details of that setup are not covered there:

- An explicit `DATABASE_ENV_FILE` names a different file than the checkout's
  `env/.env.database`, and the CLI honours it from any directory.
- `${...}` references in the `HARBORRAG_CONTROL_DB_URL` line resolve from the shell
  first, then from the same file, and are substituted verbatim.
- When a reference resolves to nothing, the CLI logs a warning, ignores the line and
  stays on the SQLite database.

## Choose a mode

| Situation | Use |
| --- | --- |
| Routine synchronization | `incremental` |
| A connector reports reliable changes and most documents are unchanged | `incremental` |
| Processing configuration changed | `incremental`; the processing fingerprint triggers the reprocessing it needs |
| Source change metadata may be stale or wrong | `force` |
| Checking a parser or connector fix against every source document | `force` |
| Retrying the failures of one finished run | `POST /v1/admin/ingestions/{ingestion_id}/retry-failures` |
| Rebuilding vector or graph projections from canonical artifacts | The `harborrag.reindex` workflow, not `force`; see [Rebuild projections](../developers/architecture/projection-rebuild.md#rebuild-projections) |
| Deleting obsolete Qdrant collections | Manual cleanup after a verified rebuild, not either mode; see [Qdrant collections](../developers/architecture/projection-rebuild.md#qdrant-collections) |

Starting a run while the connection is already ingesting answers `409 already_running`,
with `details.ingestion_id` naming the running ingestion; no `Idempotency-Key` is
needed.
