# CLI reference

The installed command is `harborrag`. Run
`uv run harborrag ...` from a source checkout or
`harborrag ...` from an installed package.

Typer provides grouped Rich help pages, validation, typo-friendly errors, and
shell completion. One-shot commands render Rich status panels, progress bars,
summaries, and actionable artifact lists. The status and final-result views
include the full ingestion stage sequence from discovery through reconciliation.
While artifacts are processed concurrently, their preflight-through-finalize
stages are marked `in flight` instead of presenting one misleading global
current stage. Disable one-shot output color with `harborrag --no-color ...`.

## Setup

```bash
harborrag init [DIR] [--provider openai|azure-openai|gemini|openai-compatible]
               [--chat-model NAME] [--embed-model NAME] [--embed-dimensions N]
               [--api-key KEY] [--api-base URL] [--connectors LIST] [--source PATH]
               [--ports-offset N] [--yes] [--force]
```

Scaffolds a project directory: `harborrag.yaml`, `.env`, `config/connectors.yaml`,
`config/models.yaml`, `config/parsers.yaml`, `config/temporal.yaml`, `docker-compose.yml`,
`.gitignore`. Prompts for anything not given as a flag unless `--yes`. When the directory
already holds scaffold files it asks whether to overwrite them (default no); `--force` skips
the question and `--yes` refuses, since accepting defaults must never overwrite. `.env` is
never overwritten - a fresh copy goes to `.env.new`.

`--connectors` picks the data sources to scaffold - `local` (a folder, connector name
`workspace`), `github`, `confluence`, `jira` - as a comma-separated list; interactively
`init` asks, then asks for each source's URL, identifiers and token (blank to fill in later).
Each selected source becomes one block in `config/connectors.yaml` and one group of variables
in `.env`; `harborrag doctor` reports any that are still blank.
Creates the source folder with a sample `README.md` when it is missing. Probes the ports
the compose file publishes and, when any is taken, shifts them all by the first free offset
(10000, 20000, 30000), rewriting `.env` to match and switching Qdrant to REST (the client
cannot derive a gRPC port from the URL); `--ports-offset N` forces a value.

Every command accepts the global `--project DIR` (or `HARBORRAG_PROJECT`); without it the CLI
walks up from the current directory to the nearest `harborrag.yaml` and runs from there,
printing `harborrag: using project DIR` on stderr whenever that is not the current directory.
A walked-to directory is only accepted when you own it and it is not world-writable (the same
rule as git's `safe.directory`), so a marker planted in `/tmp` or another shared ancestor
cannot silently reconfigure your run; `--project` is the explicit opt-in.
Precedence for settings: shell environment, then the project `.env`, then `runtime:` in
`harborrag.yaml`, then defaults.

## Chat

```bash
harborrag chat MESSAGE \
  [--tenant TENANT_ID] \
  [--session SESSION_ID] \
  [--json]
```

`chat` makes one non-streaming, retrieval-grounded call using the server-owned
default system prompt. It defaults to tenant `DEFAULT`. Omit `--session` on
the first turn to generate one; pass the
returned session ID on later calls to recall the two latest completed turns.

```bash
harborrag chat "Explain HarborRAG in one paragraph." --json
```

Provider settings and credentials come from `config/models.yaml` and the
process environment; they cannot be supplied as command options. See the
[Chat guide](../chat/README.md).

## Retrieval

```bash
harborrag retrieve QUERY \
  [--tenant TENANT_ID] \
  [--top-k 1..100] \
  [--lane dense|sparse|hybrid] \
  [--filters-json JSON] \
  [--graph | --no-graph] \
  [--include-content] \
  [--include-metadata] \
  [--json]
```

Retrieval defaults to tenant `DEFAULT`, 10 hybrid results, and graph-context
observation. Content and metadata are excluded unless explicitly requested.
`--filters-json` must be a JSON object and cannot contain `tenant_id`; tenant
scope is always supplied through `--tenant`.

## Diagnostics

```bash
harborrag doctor [--temporal] [--json]
```

Runs layered checks and prints one line each: project marker, the optional Python clients
the local stack needs (with the extra that installs them), connector/parser/model
catalogs, blank URL/credential variables of remote connectors (the model catalog expands every `${VAR}`, so a blank key fails here), the source
folder of each enabled local connector, Qdrant, FalkorDB, the object store, the control
database, and - only with `--temporal` - the Temporal workflow service. Exit 1 when a
required check fails. `--json` returns `{"ok", "data": {"checks": [...], "summary": {...},
"diagnostics": {...}}, "error"}`.

## Ingestion

```bash
harborrag ingest run CONNECTOR [--tenant TENANT_ID] [--run-id RUN_ID]
  [--connection-id ID] [--source-scope-id ID] [--path PATH] [--pattern PATTERN]
  [--recursive | --no-recursive] [--attachments | --no-attachments]
  [--updated-after ISO8601] [--filters-json JSON] [--force-reprocess] [--limit COUNT]
  [--events] [--json]
```

`run` executes the ingestion in the current process (no Temporal, no worker) and draws an
inline progress block - stage strip, document bar, counters, failed artifacts - until it
finishes, then prints the run summary. Exit 1 if the run failed. `--json` prints only the
final envelope; `--events` streams one NDJSON progress object per change and ends with the
envelope.

```bash
harborrag ingest start \
  --connector-id CONNECTOR_ID \
  [--tenant TENANT_ID] \
  [--run-id RUN_ID] \
  [--connection-id CONNECTION_ID] \
  [--source-scope-id SOURCE_SCOPE_ID] \
  [--path PATH] [--pattern PATTERN] \
  [--recursive | --no-recursive] \
  [--attachments | --no-attachments] \
  [--updated-after ISO8601] \
  [--filters-json JSON] [--force-reprocess] \
  [--limit COUNT] \
  [--batch-size COUNT] [--document-concurrency COUNT] \
  [--wait] [--json]

harborrag ingest status RUN_ID [--json]
harborrag ingest wait RUN_ID [--json]
harborrag ingest watch RUN_ID [--refresh SECONDS] [--events]
harborrag ingest pause RUN_ID [--json]
harborrag ingest resume RUN_ID [--json]
harborrag ingest cancel RUN_ID [--json]
```

`--connector-id` is the only required option; `--connector` is an accepted alias for it.
The value is a connection name from `config/connectors.yaml`, not a source type, so
`--connector-id local` fails with the list of valid IDs. `--tenant` defaults to `DEFAULT`.

`--batch-size` (1–300) and `--document-concurrency` (1–100) override the
`ingestion.batch_size` and `ingestion.document_concurrency` values in
`config/temporal.yaml` for one run. `--updated-after` takes an ISO-8601 timestamp and
limits discovery to sources changed since then. `ingest watch` has `--events` instead of
`--json`: it streams NDJSON progress and ends with the result envelope.

`start` generates an omitted run ID and deterministically derives omitted
connection/scope identity. `--wait` submits the ingestion workflow and waits
for its final result. Pause, resume, and cancellation take effect at safe batch
boundaries. A new run for the same source scope replays reusable durable
artifacts after a terminal failure.

### Following a run

`ingest start --wait` and `ingest watch RUN_ID` show the same inline progress block as
`ingest run`, polling Temporal every `--refresh` seconds (0.25–60, default 1) until the run
settles, then print the final summary. Ctrl-C leaves the run untouched; use
`ingest pause|resume|cancel` to control it. `--events` (on `watch`) streams NDJSON instead.

JSON output uses a stable envelope:

```json
{"ok":true,"data":{},"error":null}
```

Successful commands exit with 0 and failed runtime operations exit with 1.
JSON mode suppresses all Rich spinners, color, and decorative output, making it
safe to pipe directly to `jq` or another automation tool.
