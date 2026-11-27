# Upgrading

Choose your current version and follow its checklist. First-time users can start with the
[Quick Start](../getting-started/quick-start.md). See the [changelog](../../CHANGELOG.md) for
the full list of changes.

| Current version | Next step |
| --- | --- |
| `2.0.0a2` | [Upgrade to 2.0.0](#200a2-to-200) |
| `2.0.0a1` | [Upgrade to 2.0.0a2](#200a1-to-200a2) first, then [upgrade to 2.0.0](#200a2-to-200) |

## 2.0.0a2 to 2.0.0

Connections move from `config/sources/connectors.yaml` into the control database. Use the
[Connections guide](connections.md) to manage them after the upgrade. Ingestion reads each
connection when a run starts.

1. [Prepare the deployment and its configuration](#before-you-start).
2. [Review compatibility for your clients and automation](#behaviour-that-changes).
3. [Stop, back up and upgrade the services in order](#upgrade-the-services-in-order).
4. [Restore connection settings, run ingestion and check retrieval](#after-the-upgrade).

### Before you start

Plan a maintenance window. Before starting 2.0.0, follow the
[configuration checklist](upgrading-2-0-0-configuration.md#before-you-start) to check
your database capacity and encryption key, move the configuration files, update environment
variables and address settings the new release refuses. If you intend to carry over settings
as overrides, prepare [`config/settings.yaml` before first start](upgrading-2-0-0-configuration.md#runtime-settings).

Before any first full sync on 2.0.0, review
[connection changes](upgrading-2-0-0-operations.md#connections). The new file-type defaults can
remove previously indexed files. Allow the types you want to keep before that run. Plan time for
the larger first ingestion and the
[embedding refresh](upgrading-2-0-0-configuration.md#re-index-when-the-embedding-model-changes).

### Behaviour that changes

Review the references for the parts of HarborRAG you use. Update affected jobs and clients as
part of the same rollout.

| If you use | Read |
| --- | --- |
| CLI scripts, Temporal, log filters or the Python SDK | [Automation changes](upgrading-2-0-0-automation.md) |
| HTTP, MCP, chat or saved evidence filters | [HTTP and MCP client changes](upgrading-2-0-0-clients.md) |
| Existing connections or the first API start | [Operations and migration details](upgrading-2-0-0-operations.md) |

### Upgrade the services in order

**Keep a backup before starting the new API.** Migrations `0044`, `0045` and `0049`
delete data a downgrade cannot restore. Migrations run in one transaction and can lock busy tables.
See [migration changes and lock requirements](upgrading-2-0-0-operations.md#control-database-migrations).

**Keep one API process per deployment during and after the upgrade.** Live ingestion
progress events stay within the process that publishes them. See
[Deployment](../developers/deployment/README.md#run-the-api-and-worker).

1. **Pause schedules and stop the services.** Cancel any run that 2.0.0a2 paused while
   a batch was running. [See the Temporal changes](upgrading-2-0-0-automation.md#cli-dependencies-and-temporal).
   Stop the API, the MCP servers and every worker. Other runs in progress stay in Temporal
   and go on when the workers return. Do not start an inline run
   (`harborrag ingestions start --inline`) until the upgrade is done: it migrates the
   database as the API does. In a checkout, stop the MCP servers with
   `scripts/deployment/mcp.sh down` and `scripts/deployment/mcp-ui.sh down` before you
   pull the new release: from then on, every `mcp.sh` command stops until the MCP
   passwords exist.
2. **Back up the control database.** `0044`, `0045` and `0049` delete data that a downgrade
   does not bring back.
3. **Start the API process** on the new release with the other services still stopped. Give its start-up and
   readiness probes enough time for the migrations: an orchestrator that restarts the
   API part way rolls the migrations back and starts them again. Wait until `/readyz`
   answers `200`.
4. **Start the workers** on the new release. The worker records the cancellation of a
   retry run, so with an older worker a retry run cancelled through the API ends
   `failed`, or stays `running` until the API next settles it, for example at the next
   start of its connection. If your workers are split by task queue, start the ones that
   serve `harborrag-io` no later than those that serve `harborrag-transform`: a 2.0.0
   document retry workflow gives its release a 2-minute heartbeat timeout, and an older
   `harborrag-io` worker never heartbeats, so every attempt times out. The shipped
   Compose worker serves every queue.
5. **Create the MCP server's read-only accounts** now that the database is migrated. With
   the Compose stack, run:

    ```bash
    scripts/deployment/dev.sh mcp-access
    ```

    It writes `POSTGRES_MCP_PASSWORD` and `MINIO_MCP_PASSWORD` to `env/.env.database`
    when they are blank, then creates or refreshes the `harborrag_mcp` role and the
    `harborrag-mcp` MinIO user with them. Run it again after every later upgrade and
    after `dev.sh down --volumes`. Elsewhere, create the accounts as
    [Read-only database and object-store accounts](detailed-guides/mcp-server/server-operations.md#read-only-database-and-object-store-accounts)
    shows.
6. **Start the MCP servers** after rebuilding them on the new release. In a checkout, run
   `scripts/deployment/mcp.sh --build --http`, and
   `scripts/deployment/mcp-ui.sh --build --http` if you use the Explorer. A preflight line
   that ends in `(cause: InvalidPasswordError)` means `mcp-access` has not run.

The new workers replay runs started before the upgrade. Some closed 2.0.0a1 batch
workflows cannot replay. See [Workflow replay](upgrading-2-0-0-automation.md#workflow-replay).

### After the upgrade

Complete these checks before returning connections to their normal schedules:

1. [Set cleared credentials again](upgrading-2-0-0-operations.md#set-cleared-credentials-again) and
   [restore significant spaces in folder and glob settings](upgrading-2-0-0-operations.md#put-back-spaces-in-folder-and-glob-settings).
   Review [what happens at the first API start](upgrading-2-0-0-operations.md#what-happens-at-the-first-start) if you
   expected a YAML import, or [the conversion of old source rows](upgrading-2-0-0-operations.md#rows-from-apiv1sources)
   if you already managed sources through the API.
2. [Run the planned force ingestion](upgrading-2-0-0-configuration.md#re-index-when-the-embedding-model-changes) for every
   connection. That also covers the [SharePoint refresh](upgrading-2-0-0-operations.md#re-ingest-sharepoint-connections-once)
   and [full Jira scan](upgrading-2-0-0-operations.md#run-each-jira-connection-once-in-full). Expect the
   [first run to do more work](upgrading-2-0-0-operations.md#expect-a-larger-first-run).
3. Confirm the runs succeeded. Check an expected citation from chat or MCP if you use them,
   then resume your normal ingestion schedules.

[Known limitations](upgrading-2-0-0-operations.md#known-limitations) covers issues to watch for while verifying the upgrade.

## 2.0.0a1 to 2.0.0a2

Alpha2 keeps each tenant's data under one storage namespace across Qdrant, FalkorDB and
the object store, and it changes how documents are projected into the graph. Stored data
is not migrated, so plan a reset and a full re-ingest.

### 1. Before you upgrade

1. Stop schedules and API submissions. Let every running HarborRAG workflow finish, or
   terminate them with the [Temporal CLI](https://docs.temporal.io/cli):

    ```bash
    temporal workflow terminate --query 'WorkflowType STARTS_WITH "harborrag"'
    ```

    An alpha1 run resumed by an alpha2 worker cannot find its plans and artifacts.

2. Back up the PostgreSQL control database. Restoring that backup, together with the
   untouched alpha1 stores, is the only practical rollback: alpha1 cannot start against
   a migrated database, and downgrading the schema loses data.

### 2. Update configuration

1. **Storage namespace.** Unset `HARBORRAG_QDRANT_COLLECTION_PREFIX` and
   `HARBORRAG_FALKORDB_TENANT_GRAPH_PREFIX`; they are ignored with a startup warning.
   Set `HARBORRAG_STORAGE_NAMESPACE_PREFIX` instead (default `harborrag`), and give each
   deployment its own value when they share stores. See
   [One tenant namespace in every store](configuration/workspace-mode.md#one-tenant-namespace-in-every-store).
2. **MCP limits.** Review `config/mcp/server.yaml`. Graph `max_depth` above 4 and
   `max_paths` above 5 are lowered with a warning.
3. **Confluence.** Remove `max_child_pages` from Confluence connectors; it is ignored
   with a warning.
4. **SharePoint.** Grant the app `Sites.Selected` on the configured sites only. A token
   with a broad role such as `Sites.Read.All` is refused at startup. Only `docx`, `pdf`,
   `pptx`, `xlsx`, `txt` and `md` files are ingested by default; set
   `allowed_extensions` to add others. For `auth_mode: device_code` on the Docker stack,
   rebuild the worker image, then run `scripts/deployment/dev.sh sharepoint-login`. See
   [SharePoint crawl scope, allowlists, and skips](configuration/connector-config.md#sharepoint-crawl-scope-allowlists-and-skips).

### 3. Clear the alpha1 data

Keep the alpha1 Qdrant collections, FalkorDB graphs and object-store keys until you verify
the upgrade if you may need to roll back. Otherwise, you can remove them during the
upgrade. Alpha2 uses these names:

| Store | Name |
| --- | --- |
| Qdrant collection | `{prefix}_{tenant}_{index}` |
| FalkorDB graph | `{prefix}_{tenant}` |
| Object-store keys | under `{prefix}_{tenant}/` |

### 4. Migrate the control database

Every HarborRAG process that opens the control database applies pending migrations at
startup. Start one process first (for example the API), wait until it is healthy, then
start the worker and the other services. The schema moves from revision 0019 to 0040; on
a large control database, migrations 0021 and 0037 hold table locks for a while.

### 5. Re-ingest

Re-ingest every source. The graph projection changed, so every document is reprocessed
without `--force-reprocess`.

Jira filter keys for custom fields with accented names have changed. Update saved
filters that use them.

### 6. Optional: keep entity summaries

Summarization is now off by default. To keep entity summaries on 2.0.0a2, set
`summarization.enabled` to `true` in `config/graph/build.yaml`, then run the 2.0.0a2
backfill command, `harborrag topology summaries backfill`.

That command exists only in 2.0.0a2. 2.0.0 removed it with the rest of
`harborrag topology`, so do not run it on 2.0.0 or later.

<details markdown="1"><summary id="detailed-sections-from-the-upgrade-guide">Detailed sections from the upgrade guide</summary>

Use these links to jump directly to the configuration steps or compatibility details you need.

### Configuration

- <a id="move-the-configuration-files" name="move-the-configuration-files"></a>[Move the configuration files](upgrading-2-0-0-configuration.md#move-the-configuration-files)
- <a id="rename-the-tenant-variable" name="rename-the-tenant-variable"></a>[Rename the tenant variable](upgrading-2-0-0-configuration.md#rename-the-tenant-variable)
- <a id="set-the-corpus-mode-for-chat" name="set-the-corpus-mode-for-chat"></a>[Set the corpus mode for chat](upgrading-2-0-0-configuration.md#set-the-corpus-mode-for-chat)
- <a id="set-the-local-source-root" name="set-the-local-source-root"></a>[Set the local source root](upgrading-2-0-0-configuration.md#set-the-local-source-root)
- <a id="set-the-model-endpoint-variables" name="set-the-model-endpoint-variables"></a>[Set the model endpoint variables](upgrading-2-0-0-configuration.md#set-the-model-endpoint-variables)
- <a id="re-index-when-the-embedding-model-changes" name="re-index-when-the-embedding-model-changes"></a>[Re-index when the embedding model changes](upgrading-2-0-0-configuration.md#re-index-when-the-embedding-model-changes)
- <a id="check-the-settings-that-are-now-refused" name="check-the-settings-that-are-now-refused"></a>[Check the settings that are now refused](upgrading-2-0-0-configuration.md#check-the-settings-that-are-now-refused)
- <a id="parser-configuration" name="parser-configuration"></a>[Parser configuration](upgrading-2-0-0-configuration.md#parser-configuration)
- <a id="runtime-settings" name="runtime-settings"></a>[Runtime settings](upgrading-2-0-0-configuration.md#runtime-settings)
- <a id="use-docling-for-pdfs" name="use-docling-for-pdfs"></a>[Use docling for PDFs](upgrading-2-0-0-configuration.md#use-docling-for-pdfs)

### Operations

- <a id="set-cleared-credentials-again" name="set-cleared-credentials-again"></a>[Set cleared credentials again](upgrading-2-0-0-operations.md#set-cleared-credentials-again)
- <a id="put-back-spaces-in-folder-and-glob-settings" name="put-back-spaces-in-folder-and-glob-settings"></a>[Put back spaces in folder and glob settings](upgrading-2-0-0-operations.md#put-back-spaces-in-folder-and-glob-settings)
- <a id="re-ingest-sharepoint-connections-once" name="re-ingest-sharepoint-connections-once"></a>[Re-ingest SharePoint connections once](upgrading-2-0-0-operations.md#re-ingest-sharepoint-connections-once)
- <a id="run-each-jira-connection-once-in-full" name="run-each-jira-connection-once-in-full"></a>[Run each Jira connection once in full](upgrading-2-0-0-operations.md#run-each-jira-connection-once-in-full)
- <a id="expect-a-larger-first-run" name="expect-a-larger-first-run"></a>[Expect a larger first run](upgrading-2-0-0-operations.md#expect-a-larger-first-run)
- <a id="paths-with-unusual-spacing-scan-again" name="paths-with-unusual-spacing-scan-again"></a>[Paths with unusual spacing scan again](upgrading-2-0-0-operations.md#paths-with-unusual-spacing-scan-again)
- <a id="files-with-decomposed-accents-are-processed-once-again" name="files-with-decomposed-accents-are-processed-once-again"></a>[Files with decomposed accents are processed once again](upgrading-2-0-0-operations.md#files-with-decomposed-accents-are-processed-once-again)
- <a id="what-happens-at-the-first-start" name="what-happens-at-the-first-start"></a>[What happens at the first start](upgrading-2-0-0-operations.md#what-happens-at-the-first-start)
- <a id="rows-from-apiv1sources" name="rows-from-apiv1sources"></a>[Rows from `/api/v1/sources`](upgrading-2-0-0-operations.md#rows-from-apiv1sources)
- <a id="connections" name="connections"></a>[Connections](upgrading-2-0-0-operations.md#connections)
- <a id="known-limitations" name="known-limitations"></a>[Known limitations](upgrading-2-0-0-operations.md#known-limitations)

### Automation

- <a id="update-scripts-for-cli-v200" name="update-scripts-for-cli-v200"></a>[Update scripts for CLI v2.0.0](upgrading-2-0-0-automation.md#update-scripts-for-cli-v200)
- <a id="rename-the-commands" name="rename-the-commands"></a>[Rename the commands](upgrading-2-0-0-automation.md#rename-the-commands)
- <a id="name-a-connection-by-its-ref" name="name-a-connection-by-its-ref"></a>[Name a connection by its ref](upgrading-2-0-0-automation.md#name-a-connection-by-its-ref)
- <a id="replace-the-flags-that-changed" name="replace-the-flags-that-changed"></a>[Replace the flags that changed](upgrading-2-0-0-automation.md#replace-the-flags-that-changed)
- <a id="read-errors-and-exit-codes-again" name="read-errors-and-exit-codes-again"></a>[Read errors and exit codes again](upgrading-2-0-0-automation.md#read-errors-and-exit-codes-again)
- <a id="update-scripts-that-read-auth-keys-output" name="update-scripts-that-read-auth-keys-output"></a>[Update scripts that read `auth keys` output](upgrading-2-0-0-automation.md#update-scripts-that-read-auth-keys-output)
- <a id="check-what-a-write-does" name="check-what-a-write-does"></a>[Check what a write does](upgrading-2-0-0-automation.md#check-what-a-write-does)
- <a id="cli-dependencies-and-temporal" name="cli-dependencies-and-temporal"></a>[CLI, dependencies and Temporal](upgrading-2-0-0-automation.md#cli-dependencies-and-temporal)
- <a id="logger-names" name="logger-names"></a>[Logger names](upgrading-2-0-0-automation.md#logger-names)
- <a id="file-names-in-logs" name="file-names-in-logs"></a>[File names in logs](upgrading-2-0-0-automation.md#file-names-in-logs)
- <a id="python-import-paths-and-service-methods" name="python-import-paths-and-service-methods"></a>[Python import paths and service methods](upgrading-2-0-0-automation.md#python-import-paths-and-service-methods)
- <a id="removed-symbols-and-their-replacements" name="removed-symbols-and-their-replacements"></a>[Removed symbols and their replacements](upgrading-2-0-0-automation.md#removed-symbols-and-their-replacements)
- <a id="mcp-server-built-in-python" name="mcp-server-built-in-python"></a>[MCP server built in Python](upgrading-2-0-0-automation.md#mcp-server-built-in-python)
- <a id="connectors-built-outside-the-runtime" name="connectors-built-outside-the-runtime"></a>[Connectors built outside the runtime](upgrading-2-0-0-automation.md#connectors-built-outside-the-runtime)

### HTTP and MCP clients

- <a id="mcp-server" name="mcp-server"></a>[MCP server](upgrading-2-0-0-clients.md#mcp-server)
- <a id="evidence-filters" name="evidence-filters"></a>[Evidence filters](upgrading-2-0-0-clients.md#evidence-filters)
- <a id="http-api-changes" name="http-api-changes"></a>[HTTP API changes](upgrading-2-0-0-clients.md#http-api-changes)
- <a id="routes-errors-and-limits" name="routes-errors-and-limits"></a>[Routes, errors and limits](upgrading-2-0-0-clients.md#routes-errors-and-limits)
- <a id="access-under-source_acl" name="access-under-source_acl"></a>[Access under `source_acl`](upgrading-2-0-0-clients.md#access-under-source_acl)
- <a id="ingestions-and-failures" name="ingestions-and-failures"></a>[Ingestions and failures](upgrading-2-0-0-clients.md#ingestions-and-failures)
- <a id="settings-schema" name="settings-schema"></a>[Settings schema](upgrading-2-0-0-clients.md#settings-schema)
- <a id="chat" name="chat"></a>[Chat](upgrading-2-0-0-clients.md#chat)

</details>
