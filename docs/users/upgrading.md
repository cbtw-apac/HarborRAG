# Upgrading

This page lists the steps each release needs when you upgrade an existing deployment,
newest release first. The [changelog](../../CHANGELOG.md) lists everything that changed;
entries marked **Breaking** there are the ones these steps deal with.

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
