# CLI reference

The installed command is `harborrag`. Run
`uv run --package harborrag-app harborrag ...` from a source checkout or
`harborrag ...` from an installed package.

Typer provides grouped Rich help pages, validation, typo-friendly errors, and
shell completion. One-shot commands render Rich status panels, progress bars,
summaries, and actionable artifact lists. The status and final-result views
include the full ingestion stage sequence from discovery through reconciliation.
While artifacts are processed concurrently, their preflight-through-finalize
stages are marked `in flight` instead of presenting one misleading global
current stage. Disable one-shot output color with `harborrag --no-color ...`.

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
harborrag doctor [--json]
```

`doctor` performs a live Temporal connection and workflow-service health check.

## Ingestion

```bash
harborrag ingest start \
  --tenant TENANT_ID \
  --connector CONNECTOR_NAME \
  [--run-id RUN_ID] \
  [--connection-id CONNECTION_ID] \
  [--source-scope-id SOURCE_SCOPE_ID] \
  [--path PATH] [--pattern PATTERN] \
  [--recursive | --no-recursive] \
  [--attachments | --no-attachments] \
  [--filters-json JSON] [--force-reprocess] \
  [--limit COUNT] \
  [--wait] [--json]

harborrag ingest status RUN_ID [--json]
harborrag ingest wait RUN_ID [--json]
harborrag ingest watch RUN_ID [--refresh SECONDS]
harborrag ingest pause RUN_ID [--json]
harborrag ingest resume RUN_ID [--json]
harborrag ingest cancel RUN_ID [--json]
```

`start` generates an omitted run ID and deterministically derives omitted
connection/scope identity. `--wait` submits the ingestion workflow and waits
for its final result. Pause, resume, and cancellation take effect at safe batch
boundaries. A new run for the same source scope replays reusable durable
artifacts after a terminal failure.

### Live dashboard

`harborrag ingest watch RUN_ID` launches a full-screen Textual control room. It
polls Temporal without blocking terminal input and presents the run overview,
pipeline stages, artifact progress, aggregate metrics, and artifacts requiring
attention. The dashboard pauses automatic polling when the run reaches a
terminal state.

Keyboard controls are displayed in the footer:

| Key | Action |
| --- | --- |
| `F` | Refresh immediately |
| `P` | Pause the workflow |
| `R` | Resume the workflow |
| `X` | Open graceful-cancellation confirmation |
| `Q` | Leave the dashboard without changing the workflow |

The polling interval defaults to one second and accepts values from 0.25 to 60
seconds. Use one-shot `status --json` rather than `watch` for scripts.

JSON output uses a stable envelope:

```json
{"ok":true,"data":{},"error":null}
```

Successful commands exit with 0 and failed runtime operations exit with 1.
JSON mode suppresses all Rich spinners, color, and decorative output, making it
safe to pipe directly to `jq` or another automation tool.
