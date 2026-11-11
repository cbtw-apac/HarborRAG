# CLI reference

Look up how the `harborrag` command maps to the HTTP API, which options every command
takes, what it prints and which exit code it returns. The usage line, arguments and
options of each command are in the generated
[CLI command reference](commands.md); `uv run harborrag COMMAND --help` prints the same.

The CLI runs from inside a HarborRAG repository checkout. Run every command through
[uv](https://docs.astral.sh/uv/) as `uv run harborrag …`, as the examples on this page
do, so it runs in the checkout's environment.

## How the CLI maps to the HTTP API

The CLI is a second way to call the operations behind the HTTP API. A command takes the
same input as its route, runs the same validation, tenant and role checks, prints the
same JSON and fails with the same error codes. In 2.0.0 the CLI runs in **local mode**:
it calls the operations in its own process, against the control database, Temporal and
the stores the checkout's environment names, so it needs no running API.

In local mode the CLI acts as the principal `cli:<os-user>`, with the role `owner` in
every tenant (`*`). The projects and connections it writes, and the runs it starts
through Temporal, record that principal in `created_by`, `updated_by` and
`triggered_by`. `harborrag me` shows it:

```bash
uv run harborrag me -o yaml
```

```yaml
principal:
  principal_type: user
  principal_id: cli:alex
  name: cli:alex
tenant_id: DEFAULT
tenants:
- '*'
role: owner
```

### Find the HTTP route of a command

The [CLI command reference](commands.md#commands-and-their-http-routes) lists the
operation and HTTP route each command calls, the
[CLI-only commands](commands.md#cli-only-commands) and the
[HTTP-only operations](commands.md#http-only-operations), each with the reason. Those
tables are generated from the CLI's command registry and the OpenAPI export, so they
match the code. `ingestions start --inline` is CLI-only too: it runs the pipeline in the
CLI's process without Temporal, for development.

### Names, bodies and connection refs

- Query parameters become flags with the same name in kebab case: `project_id` is
  `--project-id`, `connection_ref` is `--connection-ref`.
- A command that sends a body reads the HTTP JSON from `-f FILE`, as JSON or YAML;
  `-f -` reads it from stdin. A few flags set common fields, such as `--project-name`.
  Setting the same field with a flag and in the file is a usage error.
- A connection is named by its ref, `PROJECT_ID/CONNECTION_ID`, the same value as
  `connection_ref` over HTTP, such as `default/harborrag-workspace` or
  `engineering/jira-main`. A bare key such as `harborrag-workspace` is refused:

  ```text
  Invalid value for REF: expected PROJECT_ID/CONNECTION_ID, for example default/harborrag-workspace; list them with harborrag connections list
  ```

- `create` sends `If-None-Match: *`, so it fails with `412` when the resource exists.
  `update` reads the current `ETag` and sends it as `If-Match`, so it fails with `412`
  when someone changed the resource in between; `--if-match ETAG` pins one you read
  earlier.
- `projects update` and `connections update` replace the body, as the `PUT` does. A
  project field you leave out is cleared, so pass `--project-name` on every update.
- Lists take `--limit` (1 to 100, default 20) and `--cursor` like HTTP. `--all` follows
  `next_cursor` and prints every page as one `{"items": [...], "next_cursor": null}`.

## Global options

Every command except `auth keys` takes these options. They carry what an HTTP call
carries in its headers. Give them after the command, as in
`uv run harborrag connections list -o json`: before the command they are an unknown
option.

| Option | Environment variable | Default | HTTP equivalent |
| --- | --- | --- | --- |
| `--tenant TENANT_ID` | `HARBORRAG_TENANT_ID` | the profile's tenant, else `DEFAULT` | `X-Tenant-Id` |
| `-o`, `--output FORMAT`: `table`, `json` or `yaml` | `HARBORRAG_OUTPUT` | the profile's format, else `table` when stdout is a terminal and `json` otherwise | the response body |
| `--profile NAME` | `HARBORRAG_PROFILE` | `default` | none |
| `--config PATH` | none | `~/.config/harborrag/config.yaml` | none |
| `-y`, `--yes` | none | ask before a risky action | none |

A flag beats its environment variable, which beats the profile, which beats the built-in
default. `HARBORRAG_TENANT_ID` is also the default tenant of the API and the workers, so
the three act in the same tenant unless you pass `--tenant`.

Two options belong to `harborrag` itself and go before the command: `--no-color` turns
color off (as `NO_COLOR=1` does), and `--project DIR` names a project directory (see
[Choose the project directory](#choose-the-project-directory)).
`--install-completion` installs shell completion, and `--show-completion` prints it to
copy or customize.

### Set defaults in a profile

A profile sets a default tenant and output format. The config file holds one mapping,
`profiles`, from a profile name to the keys `tenant` and `output`:

```yaml
profiles:
  default:
    output: yaml
  engineering:
    tenant: engineering
    output: json
```

The CLI reads `default` unless `--profile` or `HARBORRAG_PROFILE` names another. A
missing file, or a missing `default` profile, sets nothing. A named profile the file
does not have, an unknown key, or a remote-mode key (`api_url`, `api_key`, which arrive
in 2.1.0) stops the command with a usage error, exit 2:

```text
remote mode (api_url) arrives in v2.1.0
```

### Confirm a write into a new tenant

A tenant ID names the tenant's storage, and the first write into a tenant creates it, so
a typo in `--tenant` would create a tenant. Before a write into a tenant that holds no
data, the CLI asks:

```text
Tenant `engineering` has no data yet; this creates it. Continue? [y/N]:
```

Without a terminal on stdin the command stops instead, with exit 2:
``Tenant `engineering` has no data yet; pass --yes to create it.`` Pass `--yes` in a
script that means to create the tenant. Answering no exits 1. This check runs only in
the CLI; the HTTP contract does not change.

`projects delete` and `connections delete` ask too, before they remove anything, and
need `--yes` without a terminal.

## Read the output

`-o json` prints the HTTP response body, serialized with the route's own response
model, so a script reads the CLI and the API the same way. `-o yaml` prints the same
object as YAML. `-o table` prints a readable view of it: a list shows its main columns,
and one resource prints as a tree. In a terminal, for example:

```bash
uv run harborrag connections list --project-id default --source-type local_file
```

```text
  connection_ref                name                  source_type   status   updated_at
 ────────────────────────────────────────────────────────────────────────────────────────────────────────
  default/harborrag-workspace   harborrag-workspace   local_file    active   2026-11-05T03:45:36.924071Z
```

When more pages follow, the table ends with a hint such as `More: --cursor <cursor>`.
Without a terminal, for example in a pipe, the default is `json`.

- A secret reads back as its status, `{set, last4, updated_at, from}`, never its value.
- A write prints the resource it returns; a `202` prints the job or the ingestion it
  started.
- A `204` prints nothing in JSON and YAML, and `Done.` in a table.

## Read errors and exit codes

Errors go to stderr. With `-o json`, an operation's error is the HTTP error envelope,
with the same `code` as the route and the invocation's `trace_id`:

```bash
uv run harborrag connections get default/no-such -o json
```

```json
{
  "error": {
    "code": "not_found",
    "message": "connection 'no-such' not found",
    "details": null,
    "trace_id": "86f50f0d085e44afa5146d8f4f477be7"
  }
}
```

`-o yaml` prints the same envelope as YAML. In table mode the error is one line, with
each field issue of a validation error and the first eight characters of the trace ID:

```text
error 404 not_found: connection 'no-such' not found (trace 3877634d…)
```

A usage error is reported before any operation runs, as Click's usage message in every
output format, and exits 2. That covers an unknown option, a malformed ref, a body file
that does not validate, a profile that cannot be used, and a write into an empty tenant
without a terminal or `--yes`.

| Exit code | Meaning | HTTP status |
| --- | --- | --- |
| `0` | Success | 2xx |
| `1` | Anything else, such as an internal error | 500 |
| `2` | Bad input, or the request is not supported | 400, 413, 422, 501, and usage errors |
| `3` | Not found | 404 |
| `4` | Conflict with the current state | 409, 412 |
| `5` | Not allowed | 401, 403 |
| `6` | Unavailable, try later | 429, 503, 504 |
| `8` | The run that `--wait` or `--watch` followed ended `failed` or `cancelled` | none |
| `130` | Interrupted with Ctrl-C | none |

`connections test` exits 0 even when its result says `"ok": false`, because the route
answers `200`; read `ok` from the output. A run that succeeded with some failed items
also exits 0.

## Run from a repository checkout

The CLI finds the checkout by walking up from the current directory to the nearest
directory that holds `config/sources/connectors.yaml`, and runs from there. When that is
not the current directory and stderr is a terminal, it says so on stderr:
`harborrag: using repository checkout DIR`. A script that reads stderr does not see the
notice. Outside a checkout and without a project directory, every command except
`doctor` stops with exit 1:

```text
harborrag: no project found. Run it inside a HarborRAG repository checkout, or pass --project DIR for a directory containing harborrag.yaml.
```

In a checkout, the CLI reads `env/.env.connector`, `env/.env.parser` and
`env/.env.models`. From `env/.env.database` it reads only the MinIO credentials, an
uncommented `HARBORRAG_CONTROL_DB_URL` line and `HARBORRAG_LOCAL_SOURCE_ROOT`. It does not
read `env/.env.api` or `env/.env.temporal`. An exported variable always wins over a
file.

A command that writes refuses the checkout's local SQLite control database (the
database that stores projects, connections and runs), exits 1 and names the fix. That
covers `projects create`, `update` and `delete`; `connections create`, `update` and
`delete`; every `ingestions` command that starts or changes a run; `failures retry`;
`settings set` without `--dry-run`; `config import --apply`; and `config export --file`.
To point the CLI at the stack's Postgres, uncomment the `HARBORRAG_CONTROL_DB_URL` line
in `env/.env.database` (the template is `env-example/.env.database.example`), or export
the variable. The CLI then also takes `HARBORRAG_SECRETS_ENCRYPTION_KEY` from that file,
and every command migrates that database, so rebuild the images after each pull before
you run a command:

```bash
scripts/deployment/dev.sh up --build
```

### Choose the project directory

A project directory holds a `harborrag.yaml` file from an earlier HarborRAG release. The
CLI still runs such a directory, before it looks for a checkout. Name it with the global
`--project DIR` (or `HARBORRAG_PROJECT`); without either, the CLI walks up from the
current directory to the nearest `harborrag.yaml` and runs from there. When that is not
the current directory and stderr is a terminal, it prints
`harborrag: using project DIR` on stderr. A walked-to directory is only accepted when
you own it and it is not world-writable (the same rule as git's `safe.directory`), so a
marker planted in `/tmp` or another shared ancestor cannot silently reconfigure your
run; `--project` is the explicit opt-in.

In a project directory, settings are read in this order of precedence: shell
environment, then the project `.env`, then `runtime:` in `harborrag.yaml`, then
defaults.

## Manage projects and connections

A project groups connections inside a tenant. Create one before you save a connection
in it:

```bash
uv run harborrag projects create engineering --project-name "Engineering"
```

A connection's body file is the same JSON as the HTTP `PUT`, in JSON or YAML:
`source_type`, `name`, `credentials`, `config` and `advanced`. A Swagger UI template
saved to a file works as is. `harborrag source-types list` describes the fields each
source type takes.

```yaml
source_type: jira
name: Jira main
credentials:
  method: api_token
  base_url: https://your-company.atlassian.net
  email: bot@example.com
  api_token: {secret_ref: env:JIRA_TOKEN}
config:
  jql: project = DEMO
advanced:
  page_size: 50
```

```bash
uv run harborrag connections test engineering/jira-main -f jira-main.yaml
uv run harborrag connections create engineering/jira-main -f jira-main.yaml
```

- A secret is a value, or `{secret_ref: env:NAME}`, in the file. Leave it out of the file
  and pass `--secret-prompt api_token` to type it without echo; never put a secret on the
  command line. A secret left out on `update` keeps its stored value.
- An `env:` reference can be set only from the CLI and `config/sources/connectors.yaml`;
  the HTTP API takes values. The worker reads `NAME` from its own environment.
- `update` replaces `config` whole, and `advanced` when the file has it; start an update
  from `harborrag connections get REF -o yaml`.
- `test` checks sign-in, reachability and a sample of items without saving. Without
  `-f` it tests the saved connection.
- `delete` removes the connection's documents, stored secrets and row in the
  background, and prints the job; `--purge` also drops its ingestion history.

The fields, statuses and errors are in [Connections](../connections.md).

## Run and control ingestion

An ingestion run reads one connection's source and indexes what changed. Start one by
the connection's ref:

```bash
uv run harborrag ingestions start default/harborrag-workspace --wait
```

`start` submits the run to Temporal and prints it, as `POST /v1/admin/ingestions`
answers. `--mode force` re-reads and re-processes everything, and `--updated-after` takes
a date-time with a UTC offset, such as `2026-11-01T00:00:00Z`. `--item-refs` answers
`501 not_supported` in 2.0.0. A second start while a run is going fails with
`409 already_running`, exit 4.

An unknown project or connection is `422 reference_not_found`, exit 2, as over HTTP.
`ingestions start` needs the connection to be stored: the CLI does not import
`config/sources/connectors.yaml`, which the API does once at its first start.

### Follow a run

`ingestions start --wait` and `ingestions get INGESTION_ID --watch` read the run every 5
seconds until it ends; they add nothing on the server. In table mode they print one line
whenever the status or a counter changes, the last one when the run ends:

```text
9d9856bb-af05-40ac-af8c-4b946019fdca  succeeded  discovered=39 processed=39 skipped=0 failed=0
```

With `-o json` or `-o yaml` they print only the final run. The command exits 0 when the
run succeeded and 8 when it ended `failed` or `cancelled`. There is no time limit: Ctrl-C
stops following, leaves the run going, prints how to re-attach and exits 130:

```text
Stopped following; the run keeps going. Re-attach with: harborrag ingestions get <ingestion_id> --watch
```

### Pause, resume, cancel and retry

`ingestions pause`, `resume` and `cancel` apply the same rules as the HTTP API and print
the run. An action the run's status does not allow is `409 invalid_state`, exit 4, and
changes nothing. `ingestions retry-failures` starts a new run of a finished run's failed
items; `failures list` and `failures retry` work across runs. The rules are in
[Start and control ingestions](../connections.md#start-and-control-ingestions).

### Run an ingestion in this process

`ingestions start REF --inline` runs the pipeline in the CLI's process, without Temporal
or a worker, for development. It refuses `--wait`, which it already does, and
`--item-refs`. It prints the run's result, `{task_id, status, discovered, published,
unchanged, failed}`, and exits 0 when `status` is `COMPLETED` or `PARTIAL`, else 8.

`--inline` reads the files on this machine, so a `local_file` connection's `source_path`
must be a directory here. It must also lie inside `HARBORRAG_LOCAL_SOURCE_ROOT` when the
CLI has one; the template of `env/.env.database` sets `/data/sources`. The stack's
connections name the folder as the containers see it: `default/harborrag-workspace`
reads `/data/sources`. On a machine without that directory, `--inline` refuses such a
connection with `422 validation_error` on `config.source_path`, exits 2 and runs
nothing:

```text
error 422 validation_error: connection default/harborrag-workspace cannot run inline; config.source_path: /data/sources is not a directory on this machine; --inline reads files from here (trace 1a2b3c4d…)
```

## Ask a grounded question

`chat` calls `POST /v1/chat/completions` with `stream: false` and prints the answer with
its numbered citations; `-o json` prints the completion body.

```bash
uv run harborrag chat "Explain HarborRAG in one paragraph." -o json
```

Without `--session-id` it asks a one-off question, which stores nothing;
`--project-id` limits a one-off question to a project. `--session-id` continues a
session created over HTTP. `--connection-ref` narrows the search to a connection, and
`--mode` picks `classic` (the default) or `instant`.

From a checkout, the CLI does not read `env/.env.temporal`, so it keeps the default
`source_acl` access mode, in which its principal may read no source, and the search
finds nothing. Export the values the worker template sets before you run `chat`:

```bash
export HARBORRAG_CORPUS_ACCESS_MODE=tenant_shared
export HARBORRAG_CORPUS_SHARED_TENANT_ID=DEFAULT
```

The routes and the answer's fields are in the [Chat guide](../chat/README.md).

## Check the setup

```bash
uv run harborrag doctor
```

`doctor` runs without the API and checks, one line each:

- the project marker;
- the optional Python clients the local stack needs;
- the connector, parser and model catalogs and the chunking policy;
- the source folder of the enabled local connector, and blank URL and credential
  variables of remote connectors;
- the control plane, Postgres, Temporal, Qdrant, the graph database and the object
  store, with the same checks as `/readyz`.

It ends with the tenants that hold data. Trimmed, in a terminal:

```text
│  ✓    connectors catalog      4 enabled: confluence-main, harborrag-workspace, jira-main, sharepoint-main  │
│  ✓    control plane           ready                                                                        │
│  ✓    postgres                ready                                                                        │
│  ✓    temporal                ready                                                                        │
│  ✓    qdrant                  ready                                                                        │
│  ✓    graph                   ready                                                                        │
│  ✓    object store            http://localhost:9000                                                        │
╰────────────────────────────────────────────────────────────────────────────────────────────────────────────╯
Tenants with data: DEFAULT
```

A failed check prints the hint that fixes it. `doctor` exits 0 when every required
check passes, 6 when only service checks failed, and 1 when a configuration check
failed. `-o json` prints `{"checks", "ready", "summary", "tenants", "diagnostics"}`.

## Read and change runtime settings

`settings schema`, `settings get` and `settings set` call the settings routes. A level is
`tenant` (the default), `project:PROJECT_ID` or `connection:PROJECT_ID/CONNECTION_ID`.

```bash
uv run harborrag settings get --effective
```

`settings set KEY=VALUE …` reads a value as JSON when it parses, such as `8`, `true`,
`null` or `"liteparse"`, and as a string otherwise. `KEY=null` removes the override at
that level, `--dry-run` shows the change without saving it, and `-f FILE` takes the
changes as a JSON or YAML object:

```bash
uv run harborrag settings set retrieval.top_k=8 --level project:engineering
```

The keys and levels are in [Runtime settings](../configuration/runtime-settings.md).

### Import and export a settings file

```bash
uv run harborrag config export --level tenant --file settings.yaml
uv run harborrag config import settings.yaml
uv run harborrag config import settings.yaml --apply
```

`config export` writes registry keys only, to stdout or to `--file PATH`, in the shape
`import` reads, and leaves out overrides of a deleted project or connection, naming each
on stderr. Without `--level` it writes every level.

`config import` prints what the file would change. For a file that sets
`retrieval.top_k: 8`, with `-o yaml`:

```yaml
applied: false
revision: null
changes:
- level: tenant
  key: retrieval.top_k
  from: 5
  to: 8
  applies: live
skipped: []
dry_run: true
```

With `--apply` it writes the changes as one revision whose source is `import`; into a tenant with no data it asks first, as
any write does, so a script passes `--yes`. Boot-only keys are reported and skipped.
`--prune` also removes stored overrides the file lacks, at any level. An invalid file
exits 2, naming the file and the key, and applies nothing.

## Manage MCP reader keys

```bash
uv run harborrag auth keys create --tenant DEFAULT --owner user-alex --name alex-laptop --expires-in 30d
uv run harborrag auth keys list --tenant DEFAULT
uv run harborrag auth keys revoke --reason "laptop replaced" --key-id KEY_ID
```

- `create` makes a hashed, tenant-bound reader key with a lifetime such as `7d` or `12h`.
  The owner is `user-<name>`, `svc-<name>` or `team-<name>`. The secret goes only to a
  new file, `~/.harborrag/keys/<name>.key` unless `--secret-output` names another, never
  to the terminal. `--migrate` applies pending control-database migrations when the key
  table is missing.
- `list` shows a tenant's keys with their state, never hashes or secrets.
- `revoke` revokes one key by ID, or with `--tenant` and `--owner` every active key of
  one owner in a tenant. The reason is recorded in the audit trail.

`auth keys` takes none of the global options; its `--tenant` is its own required option.
Lifetime limits, the key format and how the MCP server checks keys are in
[Tenant-bound reader keys and shared corpus access](../detailed-guides/mcp-server/setup-and-integration.md#tenant-bound-reader-keys-and-shared-corpus-access).
