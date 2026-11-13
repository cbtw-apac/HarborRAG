# CLI reference

Look up what each `harborrag` command does and how it behaves. The CLI runs from inside a
HarborRAG repository checkout: run every command through [uv](https://docs.astral.sh/uv/)
as `uv run harborrag …`, as the examples on this page do, so it runs in the checkout's
environment. The usage lines show the bare `harborrag`, as `--help` prints them.

`uv run harborrag COMMAND --help`, and `uv run harborrag COMMAND SUBCOMMAND --help` for a
command group, lists every option with its default; this page covers what the help text
does not. The CLI has eight commands:

| Command | What it does |
| --- | --- |
| [`harborrag doctor`](#check-the-setup) | Check project, configuration, environment and local services |
| [`harborrag auth`](#manage-mcp-reader-keys) | Create, list and revoke MCP reader keys |
| [`harborrag config`](#import-and-export-runtime-settings) | Import and export runtime settings |
| [`harborrag chat`](#ask-a-grounded-question) | Answer one message, grounded in retrieval |
| [`harborrag retrieve`](#retrieve-evidence) | Search the indexed corpus |
| [`harborrag ingest`](#run-and-control-ingestion) | Run, follow and control ingestion runs |
| [`harborrag topology`](#operate-semantic-topology-enrichment) | Configure and operate semantic topology enrichment |
| [`harborrag source`](#manage-connections) | Manage stored connections |

## Global options and output

```bash
harborrag [--no-color] [--project DIR] COMMAND ...
harborrag --install-completion | --show-completion
```

Commands print status panels, progress bars, summaries and lists of the artifacts to act
on. `--no-color` turns color off. The ingestion status and result views list every stage
from discovery through reconciliation; while documents are processed concurrently, their
preflight-through-finalize stages show `in flight` instead of one global current stage.
`--install-completion` installs shell completion, and `--show-completion` prints it to
copy or customize.

### Run from a repository checkout

The CLI finds the checkout by walking up from the current directory to the nearest
directory that holds `config/sources/connectors.yaml`, and runs from there. It prints
`harborrag: using repository checkout DIR` on stderr whenever that is not the current
directory. Outside a checkout and without a project directory, every command except
`doctor` stops with:

```text
harborrag: no project found. Run it inside a HarborRAG repository checkout, or pass --project DIR for a directory containing harborrag.yaml.
```

In a checkout, the CLI reads `env/.env.connector`, `env/.env.parser` and `env/.env.models`.
From `env/.env.database` it reads only the MinIO credentials and an uncommented
`HARBORRAG_CONTROL_DB_URL` line. It does not read `env/.env.api` or `env/.env.temporal`.
An exported variable always wins over a file.

Commands that write refuse the checkout's local SQLite control database (the database
that stores projects, connections and runs): `ingest run`, `ingest start`, `source add`,
`source update`, `source sync`, `source remove`, `config import --apply` and
`config export --output`. To point the CLI at the stack's Postgres, uncomment the
`HARBORRAG_CONTROL_DB_URL` line in `env/.env.database` (the template is
`env-example/.env.database.example`), or export the variable. The CLI then also takes
`HARBORRAG_SECRETS_ENCRYPTION_KEY` from that file, and every command migrates that
database, so rebuild the images after each pull before you run a command:

```bash
scripts/deployment/dev.sh up --build
```

Retrieval from a checkout needs two more variables; see
[Retrieve evidence](#retrieve-evidence).

### Choose the project directory

A project directory holds a `harborrag.yaml` file from an earlier HarborRAG release. The
CLI still runs such a directory, before it looks for a checkout. Name it with the global
`--project DIR` (or `HARBORRAG_PROJECT`); without either, the CLI walks up from the
current directory to the nearest `harborrag.yaml` and runs from there, printing
`harborrag: using project DIR` on stderr whenever that is not the current directory. A
walked-to directory is only accepted when you own it and it is not world-writable (the
same rule as git's `safe.directory`), so a marker planted in `/tmp` or another shared
ancestor cannot silently reconfigure your run; `--project` is the explicit opt-in.

In a project directory, settings are read in this order of precedence: shell
environment, then the project `.env`, then `runtime:` in `harborrag.yaml`, then
defaults.

## Ask a grounded question

```bash
harborrag chat MESSAGE \
  [--tenant TENANT_ID] \
  [--session SESSION_ID] \
  [--project PROJECT_ID] \
  [--json]
```

`chat` makes one non-streaming, retrieval-grounded call using the server-owned default
system prompt. It defaults to tenant `DEFAULT`. Omit `--session` on the first turn to
create one, in the project `--project` names; pass the returned session ID on later
calls to recall the three latest completed exchanges. An existing session keeps its own
project, so `--session` with `--project` is refused. Do not confuse `--project PROJECT_ID`
of `chat` with the global `--project DIR`.

```bash
uv run harborrag chat "Explain HarborRAG in one paragraph." --json
```

Provider settings and credentials come from `config/models/models.yaml` and the process
environment; they cannot be supplied as command options. Chat grounds its answer with the
same retrieval as `harborrag retrieve`, so the checkout note under
[Retrieve evidence](#retrieve-evidence) applies to it too. See the
[Chat guide](../chat/README.md).

## Retrieve evidence

```bash
harborrag retrieve QUERY \
  [--tenant TENANT_ID] \
  [--top-k 1..100] \
  [--lane dense|sparse|hybrid] \
  [--mode flat|local_semantic] \
  [--filters-json JSON] \
  [--graph | --no-graph] \
  [--include-content] \
  [--include-metadata] \
  [--json]
```

Retrieval defaults to tenant `DEFAULT`, 10 hybrid results, `flat` mode and graph-context
observation. `--mode local_semantic` adds bounded canonical semantic expansion; it is the
same mode the MCP `vector_search` tool takes (see
[MCP tools](../detailed-guides/mcp-server/README.md#evidence-and-graph-workflow)).
Content and metadata are excluded unless explicitly requested. `--filters-json` must be a
JSON object and cannot contain `tenant_id`; tenant scope is always supplied through
`--tenant`.

With `HARBORRAG_CORPUS_ACCESS_MODE=tenant_shared`, a retrieval for
`HARBORRAG_CORPUS_SHARED_TENANT_ID` reads the shared corpus; any other tenant is scoped to
its source permissions.

From a repository checkout, the CLI does not read `env/.env.temporal` or `env/.env.api`,
so it keeps the default access mode, and retrieval for `DEFAULT` returns no results.
Export the values the worker template sets before you run `retrieve` or `chat`:

```bash
export HARBORRAG_CORPUS_ACCESS_MODE=tenant_shared
export HARBORRAG_CORPUS_SHARED_TENANT_ID=DEFAULT
```

## Check the setup

```bash
harborrag doctor [--temporal] [--json]
```

`doctor` runs layered checks and prints one line each:

- the project marker;
- the optional Python clients the local stack needs, with the extra that installs them;
- the connector, parser and model catalogs;
- blank URL and credential variables of remote connectors (the model catalog expands every
  `${VAR}`, so a blank key fails here);
- the source folder of each enabled local connector;
- Qdrant, FalkorDB, the object store and the control database;
- only with `--temporal`, the Temporal workflow service.

It exits 1 when a required check fails. `--json` returns
`{"ok", "data": {"checks": [...], "summary": {...}, "diagnostics": {...}}, "error"}`.

## Manage MCP reader keys

```bash
harborrag auth keys create --tenant TENANT_ID --owner OWNER --name NAME --expires-in DURATION \
  [--secret-output PATH] [--migrate]
harborrag auth keys list --tenant TENANT_ID
harborrag auth keys revoke --reason TEXT --key-id KEY_ID
harborrag auth keys revoke --reason TEXT --tenant TENANT_ID --owner OWNER
```

- `create` makes a hashed, tenant-bound reader key with a lifetime such as `7d` or `12h`.
  `OWNER` is `user-<name>`, `svc-<name>` or `team-<name>`. The secret goes only to a new
  file, `~/.harborrag/keys/NAME.key` unless `--secret-output` names another, never to the
  terminal. `--migrate` applies pending control-database migrations when the key table is
  missing.
- `list` shows a tenant's keys with their state, never hashes or secrets.
- `revoke` revokes one key by id, or every active key of one owner in a tenant. The
  reason is recorded in the audit trail.

Lifetime limits, key format and how the MCP server checks keys are in
[Tenant-bound reader keys and shared corpus access](../detailed-guides/mcp-server/setup-and-integration.md#tenant-bound-reader-keys-and-shared-corpus-access).

## Import and export runtime settings

```bash
harborrag config import FILE [--apply] [--prune] [--tenant TENANT_ID] [--json]
harborrag config export [--level LEVEL] [--tenant TENANT_ID] [--output PATH]
```

`import` prints what `FILE` would change and writes it as one revision only with
`--apply`. Boot-only keys are reported and skipped. `--prune` also removes stored
overrides the file lacks, at any level. An invalid file exits 1 naming the file and the
key, and applies nothing.

`export` writes registry keys only, in the shape `import` reads, and leaves out overrides
of a deleted project or connection, naming each on stderr. `--level` is `tenant`,
`project:ID` or `connection:PROJECT/CONNECTION`; without it `export` writes every level.
The file shape and the keys are in [Runtime settings](../configuration/runtime-settings.md).

## Manage connections

```bash
harborrag source list [--project-id ID] [--type TYPE] [--status STATUS] [--limit N]
                      [--cursor CURSOR] [--tenant TENANT_ID] [--json]
harborrag source show PROJECT/CONNECTION [--json]
harborrag source add PROJECT/CONNECTION --file BODY [--sync-now] [--json]
harborrag source update PROJECT/CONNECTION --file BODY [--if-version N] [--json]
harborrag source test PROJECT/CONNECTION [--file BODY] [--json]
harborrag source sync PROJECT/CONNECTION [--mode incremental|force] [--json]
harborrag source remove PROJECT/CONNECTION [--purge] [--yes] [--json]
```

A connection is one configured data source in a project, stored in the control database.
`source` manages the same stored connections as
`/v1/admin/projects/{project_id}/connections/{connection_id}`, through the same service,
so validation, secrets, change records and statuses match the API. Name a connection as
`PROJECT/CONNECTION`, using the project's id, such as `default/jira-demo`. Every
subcommand takes `--tenant`, which defaults to `DEFAULT`.

`BODY` is a YAML or JSON file in the shape of the `PUT` body: `source_type`, `name`,
`credentials`, `config` and `advanced`. A Swagger UI template saved to a file works as
is. `add` and `update` need `credentials` and `config`, as the `PUT` does: `config`
replaces the stored one whole, so start an update from `harborrag source show --json`. A
secret left out of `credentials` keeps its stored value. `test` takes any part.

A secret is a value or `{secret_ref: env:NAME}` in the file; no flag takes a secret. The
CLI and `config/sources/connectors.yaml` are the only places an `env:` reference can be
set; the HTTP API takes values. The worker reads `NAME` from its own environment.

```yaml
source_type: jira
name: Demo tickets
credentials:
  method: api_token
  base_url: https://acme.atlassian.net
  email: bot@acme.com
  api_token: {secret_ref: env:JIRA_TOKEN}
config:
  jql: project = DEMO
advanced:
  page_size: 50
```

- `add` creates the connection and refuses an id that already exists; `--sync-now` starts
  the first ingestion, and exits 1 when the connection was saved but the ingestion could
  not start (`sync_error` says why).
- `update` replaces `config`, and `advanced` when the file has it. A secret left out of the
  file keeps its stored value. The write fails if someone changed the connection after it
  was read, or since `--if-version N`.
- `show` and `list` print secrets as their status (`set`, `last4`), never the value.
- `test` checks sign-in, reachability and a sample of items without saving; with `--file`
  it tests those values. It exits 1 when a check fails.
- `sync` starts an ingestion and prints the command that follows it,
  `harborrag ingest watch ID`. `--mode force` re-reads and re-processes everything. A
  second start while one runs fails.
- `remove` deletes the connection's documents, stored secrets and row in the background.
  It asks to confirm on a terminal and needs `--yes` otherwise; `--purge` also drops its
  ingestion history.

A failed call exits 1 and, for a validation error, names each field, such as
`config.space_key: required`. From a repository checkout, writes need the stack's
Postgres; see [Run from a repository checkout](#run-from-a-repository-checkout).

## Run and control ingestion

```bash
harborrag ingest run CONNECTOR [OPTIONS] [--events] [--json]
harborrag ingest start [CONNECTOR] [OPTIONS] \
  [--batch-size COUNT] [--document-concurrency COUNT] \
  [--wait] [--timeout SECONDS] [--json]

harborrag ingest status RUN_ID [--json]
harborrag ingest wait RUN_ID [--timeout SECONDS] [--json]
harborrag ingest watch RUN_ID [--refresh SECONDS] [--events] [--timeout SECONDS]
harborrag ingest pause RUN_ID [--json]
harborrag ingest resume RUN_ID [--json]
harborrag ingest cancel RUN_ID [--json]
```

An ingestion run reads one connection's source and indexes its documents. `run` and
`start` share the options that scope one run: `--tenant`, `--run-id`, `--connection-id`,
`--source-scope-id`, `--path`, `--pattern`, `--recursive`/`--no-recursive`,
`--attachments`/`--no-attachments`, `--updated-after`, `--filters-json`,
`--force-reprocess` and `--limit`. `harborrag ingest run --help` gives each one's meaning
and default.

`run` executes the ingestion in the current process (no Temporal, no worker) and draws an
inline progress block - stage strip, document bar, counters, failed artifacts - until it
finishes, then prints the run summary. Exit 1 if the run failed. `--json` prints only
the final envelope; `--events` streams one NDJSON progress object per change and ends
with the envelope.

`start` submits the ingestion workflow to Temporal. It generates an omitted run ID and
deterministically derives omitted connection and scope identity. `--wait` waits for the
final result. `--batch-size` (1–300) and `--document-concurrency` (1–100) override the
`ingestion.batch_size` and `ingestion.document_concurrency` values in
`config/temporal.yaml` for one run. `--updated-after` takes an ISO-8601 timestamp and
limits discovery to sources changed since then.

Pause lets in-flight document work finish and then takes effect before the next batch.
Resume restarts batch dispatch; cancellation releases a paused run and takes effect at
the same safe boundary. A new run for the same source scope replays reusable durable
artifacts after a terminal failure.

`pause`, `resume` and `cancel` apply the same rules as the HTTP API. A pause of a paused
run, a pause or resume of a retry run (cancel it instead), and any action on a finished
run exit 1 with `cannot <action> an ingestion that is <status>` and change nothing. They
wait for the run to reach the target status (10 seconds for pause and resume, 30 for
cancel) and then report it as it stands. The command records the operator as the actor
on the run's event trail.

From a repository checkout, `run` and `start` need the stack's Postgres; see
[Run from a repository checkout](#run-from-a-repository-checkout).

### Name the connector

`CONNECTOR` is the only required input. Give it positionally, or to `start` with
`--connector-id` (`--connector` is an accepted alias); giving both with different values
is an error. `--tenant` defaults to `DEFAULT`.

The value is the stored connection's `source_key`, the name it was stored under. Its form
depends on how the connection was made:

- A fresh import of `config/sources/connectors.yaml` keeps each YAML key, such as
  `harborrag-workspace` or `jira-main`. The file is imported once, into a fresh install
  with no project and no connection, by the API at startup or by the CLI before its first
  `ingest run` or `ingest start`. Entries land in tenant `DEFAULT`, project `default`,
  and the file is never read again: change an imported connection with
  `harborrag source update`.
- A connection created through the API or `harborrag source add`, or one that existed
  before control-database migration 0041, has the key `PROJECT/CONNECTION`, such as
  `default/jira-main`.

Submit the shipped local connector to Temporal and wait for it:

```bash
uv run harborrag ingest start harborrag-workspace --wait
```

`harborrag source sync PROJECT/CONNECTION` starts the same ingestion by project and
connection id, whatever the key. A source type is not a key:
`harborrag ingest start local` fails with `ConnectionNotFoundError`, and so does an
imported key with any `--tenant` other than `DEFAULT`.

### Follow a run

`ingest start --wait` and `ingest watch RUN_ID` show the same inline progress block as
`ingest run`, polling Temporal every `--refresh` seconds (0.25–60, default 1) until the
run settles, then print the final summary. `ingest watch` has `--events` instead of
`--json`: it streams NDJSON progress and ends with the result envelope.

`--timeout SECONDS` (also on `wait`) stops following after that long and exits 3; the
run keeps going. The timeout bounds only the wait, never the submission, and with
`--json` it prints
`{"ok":true,"data":{"run":{"run_id":"…","status":"running"},"timed_out":true},"error":null}`.
Ctrl-C (also on `wait`) leaves the run untouched and names it (exit 130); use
`ingest pause|resume|cancel` to control it.

## Operate semantic topology enrichment

`topology` groups the commands that configure and run semantic topology enrichment.
`harborrag topology COMMAND --help` holds each command's options, and
`harborrag topology GROUP SUBCOMMAND --help` those of a nested group's commands. Most
take `--tenant`, which defaults to `DEFAULT`.

| Command | What it does |
| --- | --- |
| `evaluate` | Evaluate expected/predicted extractions in JSONL offline; no provider requests. |
| `audit` | Check at most 100 eligible semantic graph builds for projection drift. |
| `cleanup` | Preview retired builds; `--apply` deletes projections while retaining frozen data. |
| `entities` | List at most 100 currently supported mentions and their opaque entity IDs. |
| `inspect` | Show LLM descriptions, typed facts, exact evidence, and frozen provenance. |
| `profile` | Print a pinned profile template from the model catalog (no model request). |
| `enable` | Enable a pinned extraction policy and enqueue unchanged active documents. |
| `disable` | Revoke serving eligibility and prevent new work for a source scope. |
| `status` | List recent jobs without returning extracted source text. |
| `run` | Drain a bounded number of durable enrichment attempts directly. |
| `worker` | Continuously reconcile and process jobs until interrupted. |
| `rebuild` | Restore an eligible graph build from immutable artifacts without an LLM. |
| `derive` | Build budgeted contextual/parent artifacts independently of accepted topology. |
| `resolution` | Audited, reversible entity identity decisions: `merge`, `revert`, `list`. |
| `indexing` | Tenant mode, spending limits and trusted resolved ACL imports: `configure`, `status`, `permissions-import`, `permissions-status`. |
| `config` | Validate and apply `config/graph/build.yaml`: `validate`, `apply`. See [Graph build configuration](../configuration/graph-build-config.md). |
| `summaries` | Published-content summary projections: `status`, `backfill`, `run-once`, `worker`, `cleanup`. |

## Read JSON output and exit codes

Every command that takes `--json` writes one stable envelope to stdout:

```json
{"ok":true,"data":{},"error":null}
```

Successful commands exit with 0 and failed runtime operations exit with 1. For an
ingestion, `ok` means the command executed; `data.result.status` and the exit code report
the run outcome (0 completed, 1 not completed, 3 timeout, 130 interrupted). JSON mode
suppresses all spinners, color and decorative output, so it is safe to pipe directly to
`jq` or another automation tool.
