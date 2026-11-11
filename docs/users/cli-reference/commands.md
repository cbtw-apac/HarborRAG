# CLI command reference

Look up the usage line, arguments and options of every `harborrag` command. How the CLI maps to the HTTP API, its global options, output formats and exit codes are in the [CLI reference](README.md).

This page is generated from the CLI's own help by `make cli-reference`; to change it, change the command's help text.

## harborrag auth keys create

Create a reader key; the secret goes only to the output file, never to the terminal.

```text
harborrag auth keys create [OPTIONS]
```

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `--tenant` | text | required | Tenant the key may read. |
| `--owner` | text | required | user-&lt;name&gt;, svc-&lt;name&gt; or team-&lt;name&gt;. |
| `--name` | text | required | Device or workload, e.g. alex-laptop. |
| `--expires-in` | text | required | Lifetime such as 30d or 12h; required. |
| `--secret-output` | path |  | New file to receive the key (mode 0600); default ~/.harborrag/keys/&lt;name&gt;.key. |
| `--migrate` | boolean | `false` | Apply pending control-plane migrations if the key table is missing. |

## harborrag auth keys list

List a tenant's keys with their state; never shows hashes or secrets.

```text
harborrag auth keys list [OPTIONS]
```

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `--tenant` | text | required | Tenant the key may read. |

## harborrag auth keys revoke

Revoke one key by id, or all active keys of an owner in a tenant (offboarding).

```text
harborrag auth keys revoke [OPTIONS]
```

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `--reason` | text | required | Recorded in the audit trail. |
| `--key-id` | text |  | One key. |
| `--tenant` | text |  |  |
| `--owner` | text |  | With --tenant: every active key of one owner. |

## harborrag chat

Generate a response with the configured chat model.

```text
harborrag chat [OPTIONS] MESSAGE
```

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `MESSAGE` | text | required | Question for the configured chat model. |
| `--session-id SESSION_ID` | text |  | Continue a session. |
| `--mode` | text |  | instant or classic (default: classic). |
| `--project-id PROJECT_ID` | text |  | One-off questions: read only this project. |
| `--connection-ref REF` | text |  | Narrow to this connection, PROJECT_ID/CONNECTION_ID; repeat for more. |
| `--tenant TENANT_ID` | text |  | Tenant to act in (env HARBORRAG_TENANT_ID; default: the profile's, else DEFAULT). |
| `--output, -o` | `table`, `json`, `yaml` |  | Print a table, JSON or YAML (env HARBORRAG_OUTPUT; default: table in a terminal, else json). |
| `--profile NAME` | text |  | Profile of the config file to use (env HARBORRAG_PROFILE; default: default). |
| `--config PATH` | file |  | Config file holding the profiles (default: ~/.config/harborrag/config.yaml). |
| `--yes, -y` | boolean | `false` | Answer yes to confirmation questions, for scripts. |

## harborrag config export

Print the stored overrides as a settings file that ``import`` reads back unchanged.

Overrides of a deleted project or connection are left out, each named on stderr.

```text
harborrag config export [OPTIONS]
```

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `--level LEVEL` | text |  | tenant, project:ID or connection:PROJECT/CONNECTION; default: every level. |
| `--file PATH` | path |  | Write here, not to stdout. |
| `--tenant TENANT_ID` | text |  | Tenant to act in (env HARBORRAG_TENANT_ID; default: the profile's, else DEFAULT). |
| `--output, -o` | `table`, `json`, `yaml` |  | Print a table, JSON or YAML (env HARBORRAG_OUTPUT; default: table in a terminal, else json). |
| `--profile NAME` | text |  | Profile of the config file to use (env HARBORRAG_PROFILE; default: default). |
| `--config PATH` | file |  | Config file holding the profiles (default: ~/.config/harborrag/config.yaml). |
| `--yes, -y` | boolean | `false` | Answer yes to confirmation questions, for scripts. |

## harborrag config import

Show what FILE would change, or write it as one revision with ``--apply``.

Boot-only keys are reported and skipped. An invalid file exits 2, naming the file and the key, and changes nothing.

```text
harborrag config import [OPTIONS] FILE
```

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `FILE` | path | required | Settings YAML file. |
| `--apply` | boolean | `false` | Write the changes; without it, print them only. |
| `--prune` | boolean | `false` | Also remove stored overrides the file lacks. |
| `--tenant TENANT_ID` | text |  | Tenant to act in (env HARBORRAG_TENANT_ID; default: the profile's, else DEFAULT). |
| `--output, -o` | `table`, `json`, `yaml` |  | Print a table, JSON or YAML (env HARBORRAG_OUTPUT; default: table in a terminal, else json). |
| `--profile NAME` | text |  | Profile of the config file to use (env HARBORRAG_PROFILE; default: default). |
| `--config PATH` | file |  | Config file holding the profiles (default: ~/.config/harborrag/config.yaml). |
| `--yes, -y` | boolean | `false` | Answer yes to confirmation questions, for scripts. |

## harborrag connections create

Create a connection from a body file; fails with 412 when it already exists.

```text
harborrag connections create [OPTIONS] REF
```

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `REF` | text | required | PROJECT_ID/CONNECTION_ID, e.g. engineering/jira-main. |
| `--file, -f BODY` | text | required | JSON or YAML body; - reads stdin. |
| `--secret-prompt FIELD` | text |  | Ask for this credential field without echo; repeat for more fields. |
| `--tenant TENANT_ID` | text |  | Tenant to act in (env HARBORRAG_TENANT_ID; default: the profile's, else DEFAULT). |
| `--output, -o` | `table`, `json`, `yaml` |  | Print a table, JSON or YAML (env HARBORRAG_OUTPUT; default: table in a terminal, else json). |
| `--profile NAME` | text |  | Profile of the config file to use (env HARBORRAG_PROFILE; default: default). |
| `--config PATH` | file |  | Config file holding the profiles (default: ~/.config/harborrag/config.yaml). |
| `--yes, -y` | boolean | `false` | Answer yes to confirmation questions, for scripts. |

## harborrag connections delete

Delete a connection, its documents and its stored secrets in the background.

```text
harborrag connections delete [OPTIONS] REF
```

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `REF` | text | required | PROJECT_ID/CONNECTION_ID, e.g. engineering/jira-main. |
| `--purge` | boolean | `false` | Also drop the connection's Postgres history. |
| `--tenant TENANT_ID` | text |  | Tenant to act in (env HARBORRAG_TENANT_ID; default: the profile's, else DEFAULT). |
| `--output, -o` | `table`, `json`, `yaml` |  | Print a table, JSON or YAML (env HARBORRAG_OUTPUT; default: table in a terminal, else json). |
| `--profile NAME` | text |  | Profile of the config file to use (env HARBORRAG_PROFILE; default: default). |
| `--config PATH` | file |  | Config file holding the profiles (default: ~/.config/harborrag/config.yaml). |
| `--yes, -y` | boolean | `false` | Answer yes to confirmation questions, for scripts. |

## harborrag connections get

Show one connection; a stored secret reads back as its status, never its value.

```text
harborrag connections get [OPTIONS] REF
```

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `REF` | text | required | PROJECT_ID/CONNECTION_ID, e.g. engineering/jira-main. |
| `--tenant TENANT_ID` | text |  | Tenant to act in (env HARBORRAG_TENANT_ID; default: the profile's, else DEFAULT). |
| `--output, -o` | `table`, `json`, `yaml` |  | Print a table, JSON or YAML (env HARBORRAG_OUTPUT; default: table in a terminal, else json). |
| `--profile NAME` | text |  | Profile of the config file to use (env HARBORRAG_PROFILE; default: default). |
| `--config PATH` | file |  | Config file holding the profiles (default: ~/.config/harborrag/config.yaml). |
| `--yes, -y` | boolean | `false` | Answer yes to confirmation questions, for scripts. |

## harborrag connections list

List connections; the ones that need attention come first.

```text
harborrag connections list [OPTIONS]
```

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `--project-id` | text |  | Only this project's connections. |
| `--source-type` | text |  | Only connections of this type. |
| `--status` | `active`, `needs_reauth`, `error` |  | Only connections in this status. |
| `--limit` | integer range | `20` | Page size, 1 to 100. |
| `--cursor` | text |  | next_cursor of the previous page. |
| `--all` | boolean | `false` | Follow next_cursor and print every page as one. |
| `--tenant TENANT_ID` | text |  | Tenant to act in (env HARBORRAG_TENANT_ID; default: the profile's, else DEFAULT). |
| `--output, -o` | `table`, `json`, `yaml` |  | Print a table, JSON or YAML (env HARBORRAG_OUTPUT; default: table in a terminal, else json). |
| `--profile NAME` | text |  | Profile of the config file to use (env HARBORRAG_PROFILE; default: default). |
| `--config PATH` | file |  | Config file holding the profiles (default: ~/.config/harborrag/config.yaml). |
| `--yes, -y` | boolean | `false` | Answer yes to confirmation questions, for scripts. |

## harborrag connections test

Check a connection's values or the saved connection; stores nothing, exits 0 on ok false.

```text
harborrag connections test [OPTIONS] REF
```

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `REF` | text | required | PROJECT_ID/CONNECTION_ID, e.g. engineering/jira-main. |
| `--file, -f BODY` | text |  | Values to check; default: the saved connection. |
| `--tenant TENANT_ID` | text |  | Tenant to act in (env HARBORRAG_TENANT_ID; default: the profile's, else DEFAULT). |
| `--output, -o` | `table`, `json`, `yaml` |  | Print a table, JSON or YAML (env HARBORRAG_OUTPUT; default: table in a terminal, else json). |
| `--profile NAME` | text |  | Profile of the config file to use (env HARBORRAG_PROFILE; default: default). |
| `--config PATH` | file |  | Config file holding the profiles (default: ~/.config/harborrag/config.yaml). |
| `--yes, -y` | boolean | `false` | Answer yes to confirmation questions, for scripts. |

## harborrag connections update

Replace a connection with a body file; config is replaced whole.

```text
harborrag connections update [OPTIONS] REF
```

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `REF` | text | required | PROJECT_ID/CONNECTION_ID, e.g. engineering/jira-main. |
| `--file, -f BODY` | text | required | JSON or YAML body; - reads stdin. |
| `--secret-prompt FIELD` | text |  | Ask for this credential field without echo; repeat for more fields. |
| `--if-match` | text |  | Update only if the connection still has this ETag. |
| `--tenant TENANT_ID` | text |  | Tenant to act in (env HARBORRAG_TENANT_ID; default: the profile's, else DEFAULT). |
| `--output, -o` | `table`, `json`, `yaml` |  | Print a table, JSON or YAML (env HARBORRAG_OUTPUT; default: table in a terminal, else json). |
| `--profile NAME` | text |  | Profile of the config file to use (env HARBORRAG_PROFILE; default: default). |
| `--config PATH` | file |  | Config file holding the profiles (default: ~/.config/harborrag/config.yaml). |
| `--yes, -y` | boolean | `false` | Answer yes to confirmation questions, for scripts. |

## harborrag doctor

Check project, configuration, environment, and local services.

```text
harborrag doctor [OPTIONS]
```

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `--tenant TENANT_ID` | text |  | Tenant to act in (env HARBORRAG_TENANT_ID; default: the profile's, else DEFAULT). |
| `--output, -o` | `table`, `json`, `yaml` |  | Print a table, JSON or YAML (env HARBORRAG_OUTPUT; default: table in a terminal, else json). |
| `--profile NAME` | text |  | Profile of the config file to use (env HARBORRAG_PROFILE; default: default). |
| `--config PATH` | file |  | Config file holding the profiles (default: ~/.config/harborrag/config.yaml). |
| `--yes, -y` | boolean | `false` | Answer yes to confirmation questions, for scripts. |

## harborrag failures list

List failed items, newest first; counts cover the whole filter, not just the page.

```text
harborrag failures list [OPTIONS]
```

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `--project-id` | text |  | Only this project's failures. |
| `--connection-ref` | text |  | Only this connection, e.g. engineering/jira-main. |
| `--stage` | text |  | Only this stage: capture, interpret, commit, derive or publish. |
| `--error-category` | text |  | Only this error category. |
| `--resolved` | boolean | `false` | List resolved failures instead of open ones. |
| `--q` | text |  | Search the failures' items. |
| `--include-total` | boolean | `false` | Also count every failure the filter matches. |
| `--limit` | integer range | `20` | Page size, 1 to 100. |
| `--cursor` | text |  | next_cursor of the previous page. |
| `--all` | boolean | `false` | Follow next_cursor and print every page as one. |
| `--tenant TENANT_ID` | text |  | Tenant to act in (env HARBORRAG_TENANT_ID; default: the profile's, else DEFAULT). |
| `--output, -o` | `table`, `json`, `yaml` |  | Print a table, JSON or YAML (env HARBORRAG_OUTPUT; default: table in a terminal, else json). |
| `--profile NAME` | text |  | Profile of the config file to use (env HARBORRAG_PROFILE; default: default). |
| `--config PATH` | file |  | Config file holding the profiles (default: ~/.config/harborrag/config.yaml). |
| `--yes, -y` | boolean | `false` | Answer yes to confirmation questions, for scripts. |

## harborrag failures retry

Retry failed items by --ids, or every one a filter matches; prints the new runs.

Give either --ids or the filter options, not both; 409 when nothing qualifies.

```text
harborrag failures retry [OPTIONS]
```

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `--ids A,B` | text |  | Retry these failures by id. |
| `--connection-ref` | text |  | Only this connection, e.g. engineering/jira-main. |
| `--stage` | text |  | Only this stage: capture, interpret, commit, derive or publish. |
| `--error-category` | text |  | Only this error category. |
| `--tenant TENANT_ID` | text |  | Tenant to act in (env HARBORRAG_TENANT_ID; default: the profile's, else DEFAULT). |
| `--output, -o` | `table`, `json`, `yaml` |  | Print a table, JSON or YAML (env HARBORRAG_OUTPUT; default: table in a terminal, else json). |
| `--profile NAME` | text |  | Profile of the config file to use (env HARBORRAG_PROFILE; default: default). |
| `--config PATH` | file |  | Config file holding the profiles (default: ~/.config/harborrag/config.yaml). |
| `--yes, -y` | boolean | `false` | Answer yes to confirmation questions, for scripts. |

## harborrag ingestions cancel

Cancel a run; 409 invalid_state when its status does not allow it.

```text
harborrag ingestions cancel [OPTIONS] INGESTION_ID
```

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `INGESTION_ID` | text | required | Run id, as ingestions list prints it. |
| `--tenant TENANT_ID` | text |  | Tenant to act in (env HARBORRAG_TENANT_ID; default: the profile's, else DEFAULT). |
| `--output, -o` | `table`, `json`, `yaml` |  | Print a table, JSON or YAML (env HARBORRAG_OUTPUT; default: table in a terminal, else json). |
| `--profile NAME` | text |  | Profile of the config file to use (env HARBORRAG_PROFILE; default: default). |
| `--config PATH` | file |  | Config file holding the profiles (default: ~/.config/harborrag/config.yaml). |
| `--yes, -y` | boolean | `false` | Answer yes to confirmation questions, for scripts. |

## harborrag ingestions get

Show one ingestion run; --watch follows it until it ends.

```text
harborrag ingestions get [OPTIONS] INGESTION_ID
```

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `INGESTION_ID` | text | required | Run id, as ingestions list prints it. |
| `--watch` | boolean | `false` | Refresh until the run ends; exit 8 unless it succeeds. |
| `--tenant TENANT_ID` | text |  | Tenant to act in (env HARBORRAG_TENANT_ID; default: the profile's, else DEFAULT). |
| `--output, -o` | `table`, `json`, `yaml` |  | Print a table, JSON or YAML (env HARBORRAG_OUTPUT; default: table in a terminal, else json). |
| `--profile NAME` | text |  | Profile of the config file to use (env HARBORRAG_PROFILE; default: default). |
| `--config PATH` | file |  | Config file holding the profiles (default: ~/.config/harborrag/config.yaml). |
| `--yes, -y` | boolean | `false` | Answer yes to confirmation questions, for scripts. |

## harborrag ingestions list

List ingestion runs, newest first.

```text
harborrag ingestions list [OPTIONS]
```

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `--project-id` | text |  | Only this project's runs. |
| `--connection-ref` | text |  | Only this connection, e.g. engineering/jira-main. |
| `--status` | text |  | Only runs that are queued, running, paused, succeeded, failed or cancelled. |
| `--created-after DATE_TIME` | text |  | Only runs started after this time. |
| `--limit` | integer range | `20` | Page size, 1 to 100. |
| `--cursor` | text |  | next_cursor of the previous page. |
| `--all` | boolean | `false` | Follow next_cursor and print every page as one. |
| `--tenant TENANT_ID` | text |  | Tenant to act in (env HARBORRAG_TENANT_ID; default: the profile's, else DEFAULT). |
| `--output, -o` | `table`, `json`, `yaml` |  | Print a table, JSON or YAML (env HARBORRAG_OUTPUT; default: table in a terminal, else json). |
| `--profile NAME` | text |  | Profile of the config file to use (env HARBORRAG_PROFILE; default: default). |
| `--config PATH` | file |  | Config file holding the profiles (default: ~/.config/harborrag/config.yaml). |
| `--yes, -y` | boolean | `false` | Answer yes to confirmation questions, for scripts. |

## harborrag ingestions pause

Pause a run; 409 invalid_state when its status does not allow it.

```text
harborrag ingestions pause [OPTIONS] INGESTION_ID
```

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `INGESTION_ID` | text | required | Run id, as ingestions list prints it. |
| `--tenant TENANT_ID` | text |  | Tenant to act in (env HARBORRAG_TENANT_ID; default: the profile's, else DEFAULT). |
| `--output, -o` | `table`, `json`, `yaml` |  | Print a table, JSON or YAML (env HARBORRAG_OUTPUT; default: table in a terminal, else json). |
| `--profile NAME` | text |  | Profile of the config file to use (env HARBORRAG_PROFILE; default: default). |
| `--config PATH` | file |  | Config file holding the profiles (default: ~/.config/harborrag/config.yaml). |
| `--yes, -y` | boolean | `false` | Answer yes to confirmation questions, for scripts. |

## harborrag ingestions resume

Resume a paused run; 409 invalid_state when its status does not allow it.

```text
harborrag ingestions resume [OPTIONS] INGESTION_ID
```

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `INGESTION_ID` | text | required | Run id, as ingestions list prints it. |
| `--tenant TENANT_ID` | text |  | Tenant to act in (env HARBORRAG_TENANT_ID; default: the profile's, else DEFAULT). |
| `--output, -o` | `table`, `json`, `yaml` |  | Print a table, JSON or YAML (env HARBORRAG_OUTPUT; default: table in a terminal, else json). |
| `--profile NAME` | text |  | Profile of the config file to use (env HARBORRAG_PROFILE; default: default). |
| `--config PATH` | file |  | Config file holding the profiles (default: ~/.config/harborrag/config.yaml). |
| `--yes, -y` | boolean | `false` | Answer yes to confirmation questions, for scripts. |

## harborrag ingestions retry-failures

Start a new run of a finished run's failed items and print it.

```text
harborrag ingestions retry-failures [OPTIONS] INGESTION_ID
```

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `INGESTION_ID` | text | required | Run id, as ingestions list prints it. |
| `--tenant TENANT_ID` | text |  | Tenant to act in (env HARBORRAG_TENANT_ID; default: the profile's, else DEFAULT). |
| `--output, -o` | `table`, `json`, `yaml` |  | Print a table, JSON or YAML (env HARBORRAG_OUTPUT; default: table in a terminal, else json). |
| `--profile NAME` | text |  | Profile of the config file to use (env HARBORRAG_PROFILE; default: default). |
| `--config PATH` | file |  | Config file holding the profiles (default: ~/.config/harborrag/config.yaml). |
| `--yes, -y` | boolean | `false` | Answer yes to confirmation questions, for scripts. |

## harborrag ingestions start

Start an ingestion of a connection and print the run, or follow it with --wait.

```text
harborrag ingestions start [OPTIONS] REF
```

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `REF` | text | required | PROJECT_ID/CONNECTION_ID, e.g. engineering/jira-main. |
| `--mode` | text | `incremental` | incremental reads what changed since the last sync; force re-reads everything. |
| `--updated-after DATE_TIME` | text |  | Re-read items changed since then, with a UTC offset: 2026-11-01T00:00:00Z. |
| `--item-refs A,B` | text |  | Only these items, e.g. DEMO-1 (not in v2.0.0). |
| `--wait` | boolean | `false` | Follow the run; exit 8 when it fails or is cancelled. |
| `--inline` | boolean | `false` | Run the pipeline in this process without Temporal (development). |
| `--tenant TENANT_ID` | text |  | Tenant to act in (env HARBORRAG_TENANT_ID; default: the profile's, else DEFAULT). |
| `--output, -o` | `table`, `json`, `yaml` |  | Print a table, JSON or YAML (env HARBORRAG_OUTPUT; default: table in a terminal, else json). |
| `--profile NAME` | text |  | Profile of the config file to use (env HARBORRAG_PROFILE; default: default). |
| `--config PATH` | file |  | Config file holding the profiles (default: ~/.config/harborrag/config.yaml). |
| `--yes, -y` | boolean | `false` | Answer yes to confirmation questions, for scripts. |

## harborrag me

Show the principal, role and tenants the CLI acts with.

```text
harborrag me [OPTIONS]
```

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `--tenant TENANT_ID` | text |  | Tenant to act in (env HARBORRAG_TENANT_ID; default: the profile's, else DEFAULT). |
| `--output, -o` | `table`, `json`, `yaml` |  | Print a table, JSON or YAML (env HARBORRAG_OUTPUT; default: table in a terminal, else json). |
| `--profile NAME` | text |  | Profile of the config file to use (env HARBORRAG_PROFILE; default: default). |
| `--config PATH` | file |  | Config file holding the profiles (default: ~/.config/harborrag/config.yaml). |
| `--yes, -y` | boolean | `false` | Answer yes to confirmation questions, for scripts. |

## harborrag projects create

Create a project; fails with 412 when it already exists.

```text
harborrag projects create [OPTIONS] PROJECT
```

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `PROJECT` | text | required | Project id, for example harborrag-workspace. |
| `--project-name` | text |  | Display name (required unless -f sets it). |
| `--project-description` | text |  | What the project holds. |
| `--file, -f BODY` | text |  | JSON or YAML body; - reads stdin. |
| `--tenant TENANT_ID` | text |  | Tenant to act in (env HARBORRAG_TENANT_ID; default: the profile's, else DEFAULT). |
| `--output, -o` | `table`, `json`, `yaml` |  | Print a table, JSON or YAML (env HARBORRAG_OUTPUT; default: table in a terminal, else json). |
| `--profile NAME` | text |  | Profile of the config file to use (env HARBORRAG_PROFILE; default: default). |
| `--config PATH` | file |  | Config file holding the profiles (default: ~/.config/harborrag/config.yaml). |
| `--yes, -y` | boolean | `false` | Answer yes to confirmation questions, for scripts. |

## harborrag projects delete

Delete a project; with --cascade the removal runs in the background and prints its job.

```text
harborrag projects delete [OPTIONS] PROJECT
```

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `PROJECT` | text | required | Project id, for example harborrag-workspace. |
| `--cascade` | boolean | `false` | Also remove its connections, documents and secrets. |
| `--tenant TENANT_ID` | text |  | Tenant to act in (env HARBORRAG_TENANT_ID; default: the profile's, else DEFAULT). |
| `--output, -o` | `table`, `json`, `yaml` |  | Print a table, JSON or YAML (env HARBORRAG_OUTPUT; default: table in a terminal, else json). |
| `--profile NAME` | text |  | Profile of the config file to use (env HARBORRAG_PROFILE; default: default). |
| `--config PATH` | file |  | Config file holding the profiles (default: ~/.config/harborrag/config.yaml). |
| `--yes, -y` | boolean | `false` | Answer yes to confirmation questions, for scripts. |

## harborrag projects get

Show one project.

```text
harborrag projects get [OPTIONS] PROJECT
```

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `PROJECT` | text | required | Project id, for example harborrag-workspace. |
| `--tenant TENANT_ID` | text |  | Tenant to act in (env HARBORRAG_TENANT_ID; default: the profile's, else DEFAULT). |
| `--output, -o` | `table`, `json`, `yaml` |  | Print a table, JSON or YAML (env HARBORRAG_OUTPUT; default: table in a terminal, else json). |
| `--profile NAME` | text |  | Profile of the config file to use (env HARBORRAG_PROFILE; default: default). |
| `--config PATH` | file |  | Config file holding the profiles (default: ~/.config/harborrag/config.yaml). |
| `--yes, -y` | boolean | `false` | Answer yes to confirmation questions, for scripts. |

## harborrag projects list

List the projects in the tenant.

```text
harborrag projects list [OPTIONS]
```

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `--q` | text |  | Search project names. |
| `--limit` | integer range | `20` | Page size, 1 to 100. |
| `--cursor` | text |  | next_cursor of the previous page. |
| `--all` | boolean | `false` | Follow next_cursor and print every page as one. |
| `--tenant TENANT_ID` | text |  | Tenant to act in (env HARBORRAG_TENANT_ID; default: the profile's, else DEFAULT). |
| `--output, -o` | `table`, `json`, `yaml` |  | Print a table, JSON or YAML (env HARBORRAG_OUTPUT; default: table in a terminal, else json). |
| `--profile NAME` | text |  | Profile of the config file to use (env HARBORRAG_PROFILE; default: default). |
| `--config PATH` | file |  | Config file holding the profiles (default: ~/.config/harborrag/config.yaml). |
| `--yes, -y` | boolean | `false` | Answer yes to confirmation questions, for scripts. |

## harborrag projects update

Replace a project's body with the flags and file, as the HTTP PUT does.

A field left out is cleared, so --project-name is required unless -f sets it.

```text
harborrag projects update [OPTIONS] PROJECT
```

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `PROJECT` | text | required | Project id, for example harborrag-workspace. |
| `--project-name` | text |  | Display name (required unless -f sets it). |
| `--project-description` | text |  | What the project holds. |
| `--file, -f BODY` | text |  | JSON or YAML body; - reads stdin. |
| `--if-match` | text |  | Update only if the project still has this ETag. |
| `--tenant TENANT_ID` | text |  | Tenant to act in (env HARBORRAG_TENANT_ID; default: the profile's, else DEFAULT). |
| `--output, -o` | `table`, `json`, `yaml` |  | Print a table, JSON or YAML (env HARBORRAG_OUTPUT; default: table in a terminal, else json). |
| `--profile NAME` | text |  | Profile of the config file to use (env HARBORRAG_PROFILE; default: default). |
| `--config PATH` | file |  | Config file holding the profiles (default: ~/.config/harborrag/config.yaml). |
| `--yes, -y` | boolean | `false` | Answer yes to confirmation questions, for scripts. |

## harborrag settings get

Show the overrides stored at a level, or every key's effective value.

```text
harborrag settings get [OPTIONS]
```

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `--level LEVEL` | text | `tenant` | tenant, project:ID or connection:PROJECT_ID/CONNECTION_ID. |
| `--effective` | boolean | `false` | Show every key's effective value and origin. |
| `--tenant TENANT_ID` | text |  | Tenant to act in (env HARBORRAG_TENANT_ID; default: the profile's, else DEFAULT). |
| `--output, -o` | `table`, `json`, `yaml` |  | Print a table, JSON or YAML (env HARBORRAG_OUTPUT; default: table in a terminal, else json). |
| `--profile NAME` | text |  | Profile of the config file to use (env HARBORRAG_PROFILE; default: default). |
| `--config PATH` | file |  | Config file holding the profiles (default: ~/.config/harborrag/config.yaml). |
| `--yes, -y` | boolean | `false` | Answer yes to confirmation questions, for scripts. |

## harborrag settings schema

Describe every setting: type, range, levels and when a change applies.

```text
harborrag settings schema [OPTIONS]
```

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `--tenant TENANT_ID` | text |  | Tenant to act in (env HARBORRAG_TENANT_ID; default: the profile's, else DEFAULT). |
| `--output, -o` | `table`, `json`, `yaml` |  | Print a table, JSON or YAML (env HARBORRAG_OUTPUT; default: table in a terminal, else json). |
| `--profile NAME` | text |  | Profile of the config file to use (env HARBORRAG_PROFILE; default: default). |
| `--config PATH` | file |  | Config file holding the profiles (default: ~/.config/harborrag/config.yaml). |
| `--yes, -y` | boolean | `false` | Answer yes to confirmation questions, for scripts. |

## harborrag settings set

Save changes at a level as one revision, from KEY=VALUE pairs or a file.

```text
harborrag settings set [OPTIONS] [KEY=VALUE]...
```

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `[KEY=VALUE]...` | text |  | Changes; KEY=null removes an override. |
| `--file, -f CHANGES` | text |  | JSON or YAML object of changes. |
| `--level LEVEL` | text | `tenant` | tenant, project:ID or connection:PROJECT_ID/CONNECTION_ID. |
| `--dry-run` | boolean | `false` | Show the changes without saving them. |
| `--if-match` | text |  | Change only if the settings still have this ETag. |
| `--tenant TENANT_ID` | text |  | Tenant to act in (env HARBORRAG_TENANT_ID; default: the profile's, else DEFAULT). |
| `--output, -o` | `table`, `json`, `yaml` |  | Print a table, JSON or YAML (env HARBORRAG_OUTPUT; default: table in a terminal, else json). |
| `--profile NAME` | text |  | Profile of the config file to use (env HARBORRAG_PROFILE; default: default). |
| `--config PATH` | file |  | Config file holding the profiles (default: ~/.config/harborrag/config.yaml). |
| `--yes, -y` | boolean | `false` | Answer yes to confirmation questions, for scripts. |

## harborrag source-types list

List each type's auth methods, config_schema and advanced_schema.

```text
harborrag source-types list [OPTIONS]
```

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `--category` | `issue_tracker`, `wiki`, `files`, `code` |  | Only types of this category. |
| `--tenant TENANT_ID` | text |  | Tenant to act in (env HARBORRAG_TENANT_ID; default: the profile's, else DEFAULT). |
| `--output, -o` | `table`, `json`, `yaml` |  | Print a table, JSON or YAML (env HARBORRAG_OUTPUT; default: table in a terminal, else json). |
| `--profile NAME` | text |  | Profile of the config file to use (env HARBORRAG_PROFILE; default: default). |
| `--config PATH` | file |  | Config file holding the profiles (default: ~/.config/harborrag/config.yaml). |
| `--yes, -y` | boolean | `false` | Answer yes to confirmation questions, for scripts. |
