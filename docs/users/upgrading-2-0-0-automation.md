# Update automation for 2.0.0

Use this reference to update CLI scripts, Temporal operations, log filters and Python code when moving from 2.0.0a2 to 2.0.0.

Review the sections that apply before following the [upgrade checklist](upgrading.md#200a2-to-200).
Changes to requests and responses are in the [HTTP and MCP reference](upgrading-2-0-0-clients.md).

- [CLI commands, flags and output](#update-scripts-for-cli-v200)
- [Dependencies and Temporal](#cli-dependencies-and-temporal)
- [Logger names](#logger-names) and [file names in logs](#file-names-in-logs)
- [Python imports and service methods](#python-import-paths-and-service-methods) and
  [removed symbols](#removed-symbols-and-their-replacements)

## Update scripts for CLI v2.0.0

**Breaking:** the `harborrag` commands now follow the HTTP API. Each command calls the
same operation as its route, takes the same input, prints the same JSON and fails with
the same error codes; the [CLI reference](cli-reference/README.md) maps every command to
its route. The old names get no alias, so update every script, cron entry and runbook
that calls the CLI.

### Rename the commands

An old subcommand, such as `harborrag source list`, prints the command that replaced it
and exits 2, with `--help` too:
`error: 'harborrag source list' was renamed; use 'harborrag connections list'`.
`harborrag source` and `harborrag ingest` alone name the group that replaced them.

| Before | Now |
| --- | --- |
| `harborrag source list` | `harborrag connections list` |
| `harborrag source show REF` | `harborrag connections get REF` |
| `harborrag source add REF --file BODY` | `harborrag connections create REF -f BODY` |
| `harborrag source update REF --file BODY` | `harborrag connections update REF -f BODY` |
| `harborrag source test REF` | `harborrag connections test REF` |
| `harborrag source sync REF` | `harborrag ingestions start REF` |
| `harborrag source remove REF` | `harborrag connections delete REF` |
| `harborrag ingest start CONNECTOR` | `harborrag ingestions start REF` |
| `harborrag ingest run CONNECTOR` | `harborrag ingestions start REF --inline`, which refuses a `local_file` connection whose `source_path` is neither a file nor a directory on this machine, such as the stack's `/data/sources` |
| `harborrag ingest status RUN_ID` | `harborrag ingestions get INGESTION_ID` |
| `harborrag ingest wait RUN_ID`, `harborrag ingest watch RUN_ID` | `harborrag ingestions get INGESTION_ID --watch` |
| `harborrag ingest pause RUN_ID`, `resume`, `cancel` | `harborrag ingestions pause INGESTION_ID`, `resume`, `cancel` |

These commands are removed, with no replacement in the CLI:

- `harborrag retrieve`: search with the MCP `vector_search` tool, or ask with
  `harborrag chat`.
- `harborrag topology` and every command under it, including
  `topology summaries backfill`, `topology config validate` and `apply`, and
  `topology indexing permissions-import`. The runtime services behind them remain, but
  2.0.0 has no HTTP route or MCP tool that exposes them.
- `harborrag init`, with its scaffold templates. A project directory an earlier `init`
  created keeps working through its `harborrag.yaml`, or with `--project DIR`. Set up a
  new install from a repository checkout, as the
  [Quick Start](../getting-started/quick-start.md) does.

New commands: `me`, `projects`, `source-types list`, `ingestions list` and
`retry-failures`, `failures list` and `retry`, and `settings schema`, `get` and `set`.

### Name a connection by its ref

`ingestions start` and the `connections` commands take the connection's ref,
`PROJECT_ID/CONNECTION_ID`, where `ingest start` took its `source_key`. A connection
imported from `config/sources/connectors.yaml` sits in project `default` with its YAML
key as its ID, so `harborrag-workspace` becomes:

```bash
harborrag ingestions start default/harborrag-workspace --wait
```

A bare key is refused with exit 2 and a hint:
`Invalid value for REF: expected PROJECT_ID/CONNECTION_ID, for example default/harborrag-workspace; list them with harborrag connections list`.

The CLI no longer imports `config/sources/connectors.yaml` before its first ingestion.
The API imports it at its first start, and `ingestions start` needs the connection to
exist.

### Replace the flags that changed

- `--json` is now `-o json`, and `-o yaml` is new. `-o json` prints the HTTP response
  body, not the `{"ok", "data", "error"}` envelope. Without `-o`, the CLI prints a table
  in a terminal and JSON otherwise.
- `--force-reprocess` is now `--mode force`. `--updated-after` and `--wait` stay.
- `ingest start` and `ingest run` options that scoped a run are removed with no
  replacement: `--connector-id` and `--connector`, `--run-id`, `--connection-id`,
  `--source-scope-id`, `--path`, `--pattern`, `--recursive` and `--no-recursive`,
  `--attachments` and `--no-attachments`, `--filters-json`, `--limit`, `--batch-size` and
  `--document-concurrency`. So are `--timeout`, `--refresh` and `--events`: Ctrl-C stops
  following a run.
- `source add --sync-now` is removed: run `ingestions start` after `connections create`.
- `source update --if-version N` is now `connections update --if-match ETAG`.
- `source list --type` is now `connections list --source-type`.
- `chat --session` and `--project` are now `--session-id` and `--project-id`. Without
  `--session-id`, `chat` asks a one-off question and creates no session.
- `config import` and `config export` are now `settings import` and `settings export`,
  with no alias: `harborrag config` exits `2` with `No such command 'config'`. Their
  arguments, options and output are unchanged.
- `config export --output PATH` is now `settings export --file PATH`; `-o` is the output
  format on every command.
- `doctor --temporal` and `doctor --json` are removed: `doctor` always checks Temporal,
  and `-o json` prints its report.
- `--tenant`, `-o`, `--profile`, `--config` and `-y`/`--yes` are global options, given
  after the command. A profile in `~/.config/harborrag/config.yaml` can set the default
  tenant and output; see [Global options](cli-reference/README.md#global-options).
- The CLI's tenant variable is `HARBORRAG_TENANT_ID`, as for the API and the workers; see
  [Rename the tenant variable](upgrading-2-0-0-configuration.md#rename-the-tenant-variable).
- From a checkout, the CLI reads `HARBORRAG_LOCAL_SOURCE_ROOT` from `env/.env.database`;
  see [Set the local source root](upgrading-2-0-0-configuration.md#set-the-local-source-root).

### Read errors and exit codes again

Errors go to stderr: with `-o json` as the HTTP error envelope,
`{"error": {"code", "message", "details", "trace_id"}}`, and in a table as one line such
as `error 404 not_found: connection 'no-such' not found (trace 3877634d…)`. The exit
codes changed:

| Exit code | Before | Now |
| --- | --- | --- |
| `1` | Any failure, including a run that did not complete | Anything else, such as an internal error (`500`) |
| `2` | Usage error | Usage error or bad input (`400`, `413`, `422`, `501`); an invalid `settings import` file, which exited 1 |
| `3` | `--timeout` ran out | Not found (`404`) |
| `4` | none | Conflict (`409`, `412`), such as pausing a paused run, which exited 1 |
| `5` | none | Not allowed (`401`, `403`) |
| `6` | none | Unavailable (`429`, `503`, `504`); also `doctor` when only service checks fail |
| `8` | none | The run `--wait` or `--watch` followed ended `failed` or `cancelled`, or `connections test` reported `"ok": false` |
| `130` | Interrupted | Interrupted; the run keeps going |

`connections test` exits 8 when the check reports `"ok": false` and 0 when it passes. It
prints the same body either way, although the route answers `200`; `source test` exited 1
on a failed check.

A project the CLI cannot use, including no project found outside a checkout, follows the
same rule: without a terminal it prints the error envelope with `internal_error` and
exits 1; see [CLI, dependencies and Temporal](#cli-dependencies-and-temporal).

### Update scripts that read `auth keys` output

`harborrag auth keys create|list|revoke` printed `{"ok": true, "data": …}` and, on any
failure, `{"ok": false, "error_code": …}` with exit 1. It now prints like the other
commands:

- Success prints the object that was under `data`, such as `{"keys": [...]}` for `list`.
  Read `.keys` instead of `.data.keys`, and drop any check of `.ok`. `-o table`, `json`
  and `yaml` all work.
- A failure is the error envelope on stderr, `{"error": {"code", "message", …}}`, with
  the exit codes above: `2` for a policy violation, `3` for an unknown key, `4` when the
  database rejects the key row, and `6` when the key table is missing or behind. Branch
  on the exit code or `error.code`, not on `error_code`.
- `create` requires an explicit `--tenant` and exits 2 without it; it no longer falls
  back to `HARBORRAG_TENANT_ID`, the profile or `DEFAULT`. Only `list` keeps the CLI's
  default tenant, as [Global options](cli-reference/README.md#global-options) describes.
  A script that always passed `--tenant` keeps working. `revoke` still takes either
  `--key-id`, or `--tenant` together with `--owner`.

The secret still goes only to its `0600` file, never to the output.

### Check what a write does

- `projects update` replaces the project body, as the `PUT` does: a field left out is
  cleared, so pass `--project-name` on every update.
- A write into a tenant that holds no data asks first, so a typo in `--tenant` does not
  create a tenant. Without a terminal it stops with exit 2 unless you pass `--yes`. This
  covers `settings import --apply` into a new tenant too.
- `connections delete` and `projects delete` ask before they remove anything, and need
  `--yes` without a terminal.
- Projects and connections the CLI writes, and the runs it starts through Temporal,
  record the CLI's principal, `cli:<os-user>`, in `created_by`, `updated_by` and
  `triggered_by`.
- The notice `harborrag: using repository checkout DIR` (or `using project DIR`) prints
  only when stderr is a terminal, so a script that reads stderr sees only errors.

## CLI, dependencies and Temporal

- **The CLI runs from a checkout.** Outside a checkout or a project directory, every
  command except `doctor` stops with exit 1. In a terminal, or with `-o table`, it
  prints `harborrag: no project found. …`. The command changes are in
  [Update scripts for CLI v2.0.0](#update-scripts-for-cli-v200).
- **A project error is the error envelope away from a terminal.** **Breaking:** no
  project found, a `--project` directory without `harborrag.yaml`, a marker the CLI
  cannot read or does not trust, and an unknown key under `runtime:` print as the JSON
  or YAML error envelope, with `"code": "internal_error"` and exit 1, under `-o json` or
  `-o yaml`, under `HARBORRAG_OUTPUT`, and when stdout is not a terminal, such as in a
  pipe, in CI or with `docker run` without `-t`. Before, each printed a plain
  `harborrag: …` line. A script that reads that line from stderr passes `-o table`, or
  reads `error.message` from the envelope.
- **Dependencies match the imports.** `harborrag-runtime[temporal]` no longer installs
  `cloudpickle`, and `harborrag-runtime` no longer depends on `jsonschema`; code of your
  own that imports either must install it. `harborrag-app` declares `sqlalchemy`, its
  `api` extra declares `redis>=5,<6`, and `harborrag-adapters` and `harborrag-runtime`
  declare `python-dotenv`, which each came through another package before.
- **`harborrag ingestions pause|resume|cancel` use the HTTP rules.** An action the run's
  status does not allow exits 4 with the `invalid_state` error and sends nothing to
  Temporal: pausing a paused run, pausing or resuming a retry run, resuming a running
  run that holds no pending pause, and any action on a finished run. A run the control
  database does not hold is not found (exit 3); stop such a run in the Temporal UI.
- **One run per connection, also inline and on a schedule.** `ingestions start --inline`
  refuses a connection that has a pending, running or paused run with
  `409 already_running` and exit 4, and names that run in `details.ingestion_id`, as the
  API does. A scheduled firing is skipped while the connection has such a run, not only
  while its scan runs.
- **`qdrant-client` must be 1.17 to 1.19.** Installs of the `qdrant` extra that pinned
  an older client need `pip install -U "qdrant-client>=1.17,<1.20"`.
- **The Temporal UI shows `Paused` and `Canceled` for new source runs.** Native pause
  requires `workflow.native_pause`, Temporal Server 1.32.2 or newer, and the Python
  SDK 1.37 or newer. Earlier runs can show `Running` during a cooperative pause or
  `Completed` after a graceful cancellation. Use HarborRAG's API or CLI for pause,
  resume, and cancel so the worker also controls document dispatch.
- **Cancel runs that an earlier release paused mid-batch before you upgrade.** A run
  started before the upgrade that was paused while a batch was running keeps the old
  native pause on its later pauses, until it continues as new (every 25 batches by
  default), so pausing it again can still strand it. Cancel such runs before you
  upgrade, or clear them as below if one hangs.
- **Clear a run that hangs after a pause.** When resume and cancel change nothing,
  terminate the run's `harborrag-source:<ingestion_id>` workflow first, then its
  `harborrag-source-batch:<ingestion_id>:<n>` child, in the Temporal UI. Terminating the
  child first fails the run, and cancelling it then answers `409 invalid_state`.
  HarborRAG records the run as `cancelled` when you next cancel it or start an ingestion
  of its connection.

### Workflow replay

Runs started before the upgrade replay on the new workers. The changed workflow code sits
behind seven Temporal patch IDs, `harborrag-source-records-cancellation`,
`harborrag-document-records-cancellation`, `harborrag-source-guarded-discovery-stop`,
`harborrag-source-cancel-closed-batch`, `harborrag-retry-heartbeat-timeout`,
`harborrag-document-retry-records-cancellation` and
`harborrag-retry-counts-cancelled-document`. A batch workflow that 2.0.0a1 closed after
one of its document workflows failed does not replay. See
[Known limitations](upgrading-2-0-0-operations.md#known-limitations).

## Logger names

- **Loggers that moved with their code are renamed.** A log filter, alert rule or
  dashboard query that selects records by logger name needs the new name. Messages and
  levels are unchanged. Each logger is now named after the module that owns it, so a
  prefix that covered several modules becomes one name per module.

    | Old logger | New logger |
    | --- | --- |
    | `harborrag.app.api.chat` | `harborrag.runtime.application.chat.replay` |
    | `harborrag.app.api.readiness` | `harborrag.app.operations.readiness` |
    | `harborrag.app.workflow_control.chat` | `harborrag.runtime.application.chat.service` |
    | `harborrag.app.workflow_control.chat.query_scope` | `harborrag.runtime.application.chat.query_scope` |
    | `harborrag.app.workflow_control.composition.*` | `harborrag.runtime.application.composition.*` |
    | `harborrag.app.workflow_control.control_plane.*` | `harborrag.runtime.application.control_plane.*` |
    | `harborrag_app.workflow_control.control_plane.settings_import` (named by module, outside the `harborrag` root) | `harborrag.runtime.application.control_plane.settings_import` |
    | `harborrag.app.workflow_control.ingestion` | `harborrag.runtime.application.ingestion.service` and `harborrag.engine.ingestion.failure_inbox` |
    | `harborrag.app.workflow_control.ingestion.connection_sources`, `.direct` and `.progress_bridge` | the same names under `harborrag.runtime.application.ingestion` |
    | `harborrag.app.workflow_control.ingestion.durable`, `harborrag.app.workflow_control.memory.extraction` and `harborrag.app.workflow_control.retrieval` | removed with their modules |
    | `harborrag.app.workflow_control.memory` | `harborrag.runtime.application.memory.composition` and `.context`, `harborrag.memory.usage`, `harborrag.memory.messages` and `harborrag.memory.titles` |
    | `harborrag.app.workflow_control.memory.administration` | `harborrag.memory.administration` and `harborrag.memory.erasure` |
    | `harborrag.app.workflow_control.scheduling` | `harborrag.runtime.application.scheduling.service` |
    | `harborrag.runtime.ingestion.capture` | `harborrag.engine.ingestion.release.capture` |
    | `harborrag.runtime.ingestion.cleanup` | `harborrag.engine.ingestion.maintenance.cleanup` |
    | `harborrag.runtime.ingestion.document_pipeline` | `harborrag.engine.ingestion.release.pipeline` |
    | `harborrag.runtime.ingestion.reindex` | `harborrag.engine.ingestion.maintenance.reindex` |
    | `harborrag.runtime.ingestion.version_lifecycle` | `harborrag.engine.ingestion.release.lifecycle` |
    | `harborrag.runtime.retrieval` | `harborrag.engine.retrieval.service.service`, `harborrag.engine.retrieval.service.graph_observation` and `harborrag.engine.retrieval.service.result_loader` |
    | `harborrag.runtime.retrieval.permissions` | `harborrag.engine.retrieval.service.permissions` |
    | `harborrag.runtime.retrieval.topology` | `harborrag.engine.retrieval.service.topology` |
    | `harborrag.runtime.tools.*` | `harborrag.engine.tools.<module>` (`documents` is `document_tools`, `entities` is `find_entities`, `readers` is `reader_tools`, `sources` is `source_list_tool`; `vector_search` keeps its name; `graph_search` splits into `graph_search` and `graph_search_support`) |

## File names in logs

- **Logs and errors name a file by a digest.** Parser log lines and parser errors no
  longer contain a file name or path. They label the input as
  `input=DIGEST suffix=SUFFIX size=SIZE`, where `DIGEST` is the first 12 hex characters
  of the SHA-256 of the file name. Local and GitHub connector logs, and local load
  errors, name a path by the same kind of digest of the path. To find the lines for a
  file, compute its digest:

    ```bash
    printf '%s' 'design-review.pdf' | sha256sum | cut -c1-12
    ```

- **Temporal history keeps a failure's code, not its text.** An ingestion activity
  failure no longer carries the original error, which could quote a file name or a
  signed download URL; it keeps the failure's safe code. A PDF or OCR engine failure
  that a library raised is named by its exception class instead of the library's
  message.
- **Parser errors name the library's error class.** DOCX, ODT, PPTX, XLSX, XLS, DOC and
  PPT parser errors end with the class of the library error, such as `BadZipFile`,
  instead of its text, which could quote a path or the file's first bytes. A log search
  or alert rule that matched the old text misses the new lines. Archive-bomb errors no
  longer name the archive member, and MinerU failures show `<path>` in place of their
  temporary directories.

## Python import paths and service methods

These changes affect code that imports HarborRAG, such as an SDK caller or a worker
extension. They do not affect the HTTP API, the CLI or the MCP server. The packages keep
no alias at an old path: an old import raises `ModuleNotFoundError` or `ImportError`, and
a script that used one must import from the new path. Rewrite an import by finding the
old package in the first column and applying the pointer rule.

| Old import path (2.0.0a2) | New import path | Pointer rule |
| --- | --- | --- |
| `harborrag_adapters.connectors.schemas` | `harborrag_core.ingestion.connector_contracts` | `ConnectorQuery`, `ConnectorSkip`, `ConnectorMetadata` and `ConnectorCapabilities` are no longer importable from `harborrag_adapters.connectors` |
| `harborrag_adapters.topology.{evidence,output_policy}` | `harborrag_core.topology.<module>` | `LLMEntityExtractor` stays in `harborrag_adapters.topology` |
| `harborrag_app.api.auth.principal` | `harborrag_app.operations.principal` | |
| `harborrag_app.api.*` request and response models | `harborrag_app.operations.schemas.<resource>` | One module per resource: `chat`, `connections`, `ingestions`, `me`, `settings` and `common`. The models changed with the HTTP API, so check each field |
| `harborrag_app.workflow_control` | `harborrag_runtime.application` | Keep the rest of the path: `workflow_control.chat.service` is `application.chat.service`, and `AppResponse`, `BaseAppService` and the `AppService` composition follow the same rule. `harborrag_app` no longer exports `AppResponse` or `BaseAppService`. `chat.events`, `chat.stream_result` and `chat.turn` are removed |
| `harborrag_app.workflow_control.chat.memory_block` | `harborrag_engine.chat.memory_block` | Chat modules stay in `application.chat`, except this one |
| `harborrag_app.workflow_control.ingestion.{models,presenters,recovery}` | `harborrag_engine.ingestion.{models,presenters,recovery}` | The failure inbox is `harborrag_engine.ingestion.failure_inbox` |
| `harborrag_app.workflow_control.ingestion.progress_bridge.event_prefix` | `harborrag_core.contracts.events.event_prefix` | The rest of `progress_bridge` is `harborrag_runtime.application.ingestion.progress_bridge`. Stored and bus event names are unchanged |
| `harborrag_app.workflow_control.memory.{access,administration,erasure,messages,projects,titles,usage,locks,presenters,directory}` | `harborrag_memory.<module>` | `ModelCall`, `record_model_usage`, `SessionLocks`, `ConversationTurnBusyError`, `ConversationDirectoryService` and the presenter functions keep their names, and `harborrag_runtime.application.memory` no longer re-exports them. The error classes keep their `error_code` and HTTP status. Session and context modules stay in `harborrag_runtime.application.memory`; `extraction` is removed |
| `harborrag_app.workflow_control.memory.identity.MemoryIdentity` | `harborrag_core.domain.identity.MemoryIdentity` | `harborrag_runtime.application.memory` no longer exports it |
| `harborrag_engine.ingestion.policies` | `harborrag_core.ingestion.policies` | |
| `harborrag_runtime.ingestion.document.{models,stage_models,capture_decisions,materialization_helpers,projection_coordinator}` | `harborrag_engine.ingestion.release.<module>` | `DocumentReleaseRequest` and `DocumentReleaseOutcome` are in `release.models`; `harborrag_runtime.ingestion` and `.ingestion.document` no longer re-export them |
| `harborrag_runtime.ingestion.document.{lifecycle,dependencies,projection_material,materialization,projection}` | `harborrag_engine.ingestion.release.<module>` | `DocumentReleaseDependencies` is in `release.dependencies`, and `harborrag_runtime.ingestion` and `.ingestion.document` no longer export it. It now requires a `failures` classifier, and `parser_for` and `chunker_for` callables replace its `run_tools` field. `DocumentVersionLifecycle` requires `failures` too. `run_tools` stays in `harborrag_runtime.ingestion.document` |
| `harborrag_runtime.ingestion.document.{capture,preparation,pipeline,service}` | `harborrag_engine.ingestion.release.<module>` | Connector arguments are typed `harborrag_engine.ingestion.release.dependencies.RawDocumentLoader`, a structural type with one method, `load(record) -> RawDocument`, which every connector satisfies. The pipeline reads its stage order from `harborrag_core.ingestion.stages.DOCUMENT_STAGE_ORDER`. `DocumentReleaseService` is no longer exported from `harborrag_runtime.ingestion`, and neither it nor `DocumentStagePipeline` is exported from `.ingestion.document` |
| `harborrag_runtime.ingestion.document.normalizers.{builder,router}` | `harborrag_engine.ingestion.normalizers.<module>` | `builtins` and `transform` stay in `harborrag_runtime.ingestion.document.normalizers` |
| `harborrag_runtime.ingestion.maintenance.{reindex,reindex_reuse}` | `harborrag_engine.ingestion.maintenance.<module>` | `DocumentReindexService`, `ReindexRequest` and `source_identity_from_canonical` are no longer exported from `harborrag_runtime.ingestion`; import them from `harborrag_engine.ingestion.maintenance.reindex` |
| `harborrag_runtime.ingestion.maintenance.{cleanup,reindex_plan,relation_supersession}` | `harborrag_engine.ingestion.maintenance.<module>` | |
| `harborrag_runtime.ingestion.source.{models,tasks}` | `harborrag_engine.ingestion.source.<module>` | `SourceIngestionRequest` and `SourceIngestionOutcome` are in `source.models` |
| `harborrag_runtime.ingestion.limits`, `harborrag_runtime.source_query` | `harborrag_core.ingestion.limits`, `harborrag_core.ingestion.source_query` | `SourceQuery` and `ProcessingProfileInput` are no longer importable from `harborrag_runtime.temporal.schemas`, and `SourceSubmission` not from `harborrag_runtime.temporal.submission` |
| `harborrag_runtime.ingestion_contracts.{SourceSubmission,PreparedSourceSubmission,IngestionRetryRequest}` | `harborrag_core.ingestion.run_contracts` | `harborrag_runtime.ingestion_contracts` keeps only the execution gateway types |
| `harborrag_runtime.memory.in_memory` | `harborrag_memory.tiers.in_memory` | |
| `harborrag_runtime.retrieval.<module>` | `harborrag_engine.retrieval.service.<module>` | Applies to every module except `facades` and `composition`, which stay, and `knowledge`, which is removed; `service` is `harborrag_engine.retrieval.service.service`. `RuntimeRetrievalService.connect` is removed: call `connect_retrieval_service(settings)` from `harborrag_runtime.retrieval.composition`. `RetrievalOptions`, `RetrievalPolicy`, `RetrievalResources` and the other types of `contracts` are in `harborrag_engine.retrieval.service.contracts`; `harborrag_runtime.retrieval` no longer re-exports them |
| `harborrag_runtime.tokenization` | `harborrag_core.chunking.tokenization` | |
| `harborrag_runtime.topology.{derived_cleanup,inspection,reservation_deadline}` | `harborrag_engine.topology.<module>` | |

Removed from `BaseAppService` (`harborrag_runtime.application.ports`; `AppService` loses
them too). The other methods that 2.0.0a2 had on it for projects, sources, settings,
metrics, graph conflicts, projections, agent sessions and `ingest_once` are gone as well,
with the routes they served. The replacements are methods of `AppService`; of them,
`BaseAppService` declares only `get_task`.

| Removed method | Call instead |
| --- | --- |
| `start_ingestion(...)` | `submit(command, idempotency_key=...)` with a `harborrag_engine.ingestion.models.IngestionCreateCommand`; it registers the run and starts its workflow |
| `ingestion_status(run_id)` and `ingestion_result(run_id)` | `get_task(task_id)`, which returns the run's public task document and raises `IngestionNotFoundError` for an unknown run |
| `control_ingestion(run_id, action)` | `pause(task_id)`, `resume(task_id)` or `cancel(task_id)`, one method per action |
| `retrieve(query, ...)` | `HarborRAG().retrieval.search(RetrievalRequest(...))` of `harborrag_runtime.sdk`, or `harborrag_engine.retrieval.service.service.RuntimeRetrievalService.retrieve` |

**Breaking:** `AppService.resume(task_id)` on a running run now resumes it when its
workflow holds a pause it has not applied yet, such as one sent during discovery. On a
running run with no pending pause it raises `IngestionActionStateError`
(`invalid_state`) instead of `IngestionAlreadyRunningError` (`already_running`). Catch
the new error, or the `HarborConflictError` both derive from.

These had no production caller and are removed with no replacement. An import of one
raises `ModuleNotFoundError`, `ImportError` or `AttributeError`.

| Removed | Notes |
| --- | --- |
| `HarborRAG().knowledge.fetch_evidence`, `resolve_entities`, `lookup_entities`, `find_relations` and `find_paths` | `HarborRAG().knowledge` keeps `list_documents`, `read_evidence`, `get_document_context` and `list_sources` |
| `RuntimeRetrievalService.fetch_evidence`, `resolve_entities`, `lookup_entities`, `find_semantic_relations` and `find_semantic_paths`, and `harborrag_engine.retrieval.service.knowledge` | |
| `AppService.chat_stream` and `BaseAppService.chat_stream`, with `harborrag_runtime.application.chat.{turn,stream_result,events}` | Call `chat_completion()` or `chat_answer()`. The SDK model-level stream, `HarborRAG().chat.stream`, stays |
| The chat presenters `evidence_data`, `cited_evidence`, `chat_response_data`, `chat_stream_chunk_data` and `source_heading`, `prompting.EVIDENCE_PREAMBLE`, and `MEMORY_WARNING` and `STREAM_ERROR` of `chat.service` | |
| `harborrag_runtime.application.memory.extraction` (`MemoryExtractionQueue`, `submit_exchange`, `drain_extraction_queue`, `dropped_extractions`), and `AppService.start_memory_extraction` and `drain_memory_extraction` | Nothing queued work for it. `HARBORRAG_MEMORY_EXTRACTION_MIN_IMPORTANCE` and `HARBORRAG_MEMORY_DEDUP_THRESHOLD` are ignored. `HarborRAG().memory.extract` and `HARBORRAG_MEMORY_EXTRACTION_ENABLED` stay |
| `harborrag_adapters.repositories.cache`, `harborrag_adapters.repositories.state` and `harborrag_adapters.repositories.database.{sqlalchemy,postgresql,sqlite,client,base}` | `harborrag_adapters.repositories.database` exports only the ingestion control plane: `ChunkRepository`, `DocumentRepository`, `HarborDatabaseBackend`, `HarborUnitOfWork`, `HarborUnitOfWorkFactory`, `OutboxRepository`, `HarborDatabaseClient` and the `PostgreSQLDatabase*` and `SQLiteDatabase*` classes are gone. Their tables were never migrated, so nothing changes in the database |
| `harborrag_runtime.ingestion.{connector_metrics,discovery_metrics,observability_types}` | Import the same names from `harborrag_runtime.observability.<module>` |
| `harborrag_adapters.parsers.registry.HarborParserEngineRegistry` | Use `HarborParserRegistry` |
| `harborrag_adapters.parsers.pdf.engines.<engine>.mapping` | Import the helpers from `harborrag_adapters.parsers.pdf.normalization` |
| `harborrag_app.api.auth.dependencies.authorize_tenant` and `authorize_task_tenant`, `harborrag_app.operations.principal.Principal.tenant_scope`, and `harborrag_app.operations.schemas.ingestions.IngestionStatus` | |
| The model catalog's `profiles:` section, the `--profile` option of `python -m harborrag_adapters.models`, and the `profile` argument of `HarborChatClientConfig`, `HarborEmbedClientConfig` and `HarborRerankClientConfig` `from_file` and `from_dict` and of `prepare_config_section` | A catalog with `profiles:` is refused |
| `HarborEmbedCapabilities.max_input_tokens` | A catalog that sets it is refused |
| `harborrag_adapters.models.runtime.litellm_telemetry` and `LiteLLMTelemetryCallback` | |
| `ConnectionImportService.export` and `importing.EXPORT_VERSION` of `harborrag_runtime.config.connectors` | |
| `scripts/models/download_docling_models.py`, `download_fastembed_models.py` and `warmup_models.py`, `scripts/generate_provider_matrix.py` and `make provider-matrix` | The model scripts were empty |

Code that implements `DocumentVersionPort` must add `refresh_admission_change_key`, and
a caller of `active_relation_document_ids` must pass `tenant_id`.

## Removed symbols and their replacements

These public symbols had no caller in HarborRAG and are removed. Code of your own that
used one switches to the replacement. `ConnectorQuery` is in
`harborrag_core.ingestion.connector_contracts`.

| Removed | Use instead |
| --- | --- |
| `LocalFileConnector.load_by_paths(paths)` and `LocalFileSystem.record_for_path` | `discover(ConnectorQuery(filters={"file_paths": [...]}))`, or `ConnectorQuery(path=...)` for one file or folder, then `load(record)` for each record |
| `GitHubConnector.load_by_paths(paths)` and `GitHubRepositoryAPI.record_for_path` | `discover(ConnectorQuery(filters={"file_paths": [...]}))` (`paths` and `files` work too), then `load(record)` |
| `SharePointConnector.load_by_ids(ids)` and `SharePointDriveAPI.record_for_item_id` | `discover(ConnectorQuery(filters={"item_ids": [...]}))` (`drive_item_ids` and `file_ids` work too), then `load(record)` |
| `ConfluenceConnector.load_by_ids(ids)` | `discover(ConnectorQuery(filters={"content_ids": [...], "include_children": False}))` (`page_ids` works too), then `load(record)`; without `include_children` set to `False`, discovery also returns the pages' children |
| `HTTPRequestError` of `harborrag_adapters.connectors.exceptions` | `FetchError` |
| `HarborStorageAuthenticationError`, `HarborStorageTransactionError`, `HarborStorageMigrationError`, `HarborStorageSerializationError` and `HarborStorageIntegrityError` of `harborrag_adapters.repositories.errors` | `HarborStorageError` |
| `SummaryRepositoryPort` of `harborrag_core.ports.summary_projection` and `TopologyRepositoryPort` of `harborrag_core.ports.topology` | The focused ports each one combined, such as `SummaryReaderPort` or `TopologyJobRepositoryPort` |
| `TopologyObservationRepositoryPort` of `harborrag_core.ports.topology` | `harborrag_core.topology.search.TopologySearchPort`, with the same `active_mentions` and `active_assertions` |
| `ProjectRepositoryProvider`, `ConnectionRepositoryProvider`, `JobRepositoryProvider`, `ActivityRepositoryProvider`, `SettingsRepositoryProvider`, `ProviderRepositoryProvider`, `MemberRepositoryProvider` and `GraphConflictRepositoryProvider` | `TenantScopedRepositoryProvider[<Port>]`, such as `TenantScopedRepositoryProvider[ProjectRepositoryPort]` |
| `HarborImportError` and `HarborNotSupportedError` | `HarborError` |
| `HarborChatBudgetExceededError` and `HarborChatCancelledError` | `HarborChatError` |
| `HarborEmbedCancelledError` | `HarborEmbedError` |
| `HarborRerankCancelledError` | `HarborRerankError` |
| `authenticated_owner(request, verifier)` of `harborrag_mcp_server.server.http_auth` | `authenticated_principal(request, verifier, roles=OWNER_ROLES)` |
| `WorkflowNotRetryableError` of `harborrag_runtime.errors` | `WorkflowOperationError` |
| `ToolInvokerPort`, `SUMMARY_CARD_MAX_CHARS`, `EMBEDDING_CONTEXT_MAX_CHARS`, `EMBEDDING_CONTEXT_MAX_WORDS` and `harborrag_runtime.config.chunking.CHUNKING_CONFIG_VERSION` | Nothing; nothing read them |

Local discovery applies the connection's filters that `load_by_paths` skipped: hidden
files, include and exclude paths and globs, file types and the size limit.

`validate_buckets` and `ensure_buckets` of the S3 object store now raise
`HarborStorageTimeoutError` or `HarborStorageConnectionError` for a timeout, an
unreachable endpoint or a `5xx` answer, instead of a raw botocore error. Both derive from
`RuntimeError`, as before, and a missing or unreadable bucket is still a plain
`RuntimeError`.

## MCP server built in Python

`harborrag-mcp` and `python -m harborrag_mcp_server` read `HARBORRAG_MCP_AUDIT_PATH` once
at launch and pass the path to the server. Code that builds `McpServer(...)` itself, or
calls `create_mcp_server()` without a registry, no longer reads the variable: it writes
the audit log to the default `.harborrag/mcp-audit.jsonl`. Pass
`audit=McpAuditLog(path=...)` from `harborrag_mcp_server.audit` to choose the path; see
[Use MCP tools from Python](detailed-guides/mcp-server/tool-configuration.md#use-from-python).

## Connectors built outside the runtime

The runtime reads `HARBORRAG_CONNECTOR_ALLOWED_HOSTS` and `HARBORRAG_CONNECTOR_USE_PROXY`
once, when it composes the API or an ingestion runtime, and hands the resulting
`DestinationPolicy` to the connectors that ingestion builds: workers, inline runs such as
`ingestions start --inline`, and SDK direct mode. The connection test (`connections test`
and its API route) uses the same policy. A connector built from a config file with
`ConnectorDefinition.build` or `ConnectorCatalog.build`, or with `build_connector` or
`build_connector_from_parts` and no `destination_policy`, gets the strict default.
Connectors no longer read these variables when they open a session, and
`guarded_session` requires a policy. A GitHub, Jira, Confluence or SharePoint connector
that your own code builds with `HarborConnector` or the connector class, without the
runtime, now reaches public addresses only, whatever the environment says. To reach a
private host, pass `destination_policy=DestinationPolicy.from_environ()`, or a policy you
build, from `harborrag_adapters.connectors.policies.network`. The deployment's
environment variables and their effect do not change.
