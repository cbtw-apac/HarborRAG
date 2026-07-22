# CLI reference

The installed command is `harbor`. Run
`uv run --package harborrag-app harbor ...` from a source checkout or
`harbor ...` from an installed package.

Typer provides grouped Rich help pages, validation, typo-friendly errors, and
shell completion. One-shot commands render Rich status panels, progress bars,
summaries, and actionable artifact lists. The status and final-result views
include the full ingestion stage sequence from discovery through reconciliation.
While artifacts are processed concurrently, their preflight-through-finalize
stages are marked `in flight` instead of presenting one misleading global
current stage. Disable one-shot output color with `harbor --no-color ...`.

## Diagnostics

```bash
harbor doctor [--json]
```

`doctor` performs a live Temporal connection and workflow-service health check.

## Ingestion

```bash
harbor ingest start \
  --tenant TENANT_ID \
  --connector CONNECTOR_NAME \
  [--run-id RUN_ID] \
  [--manifest-id MANIFEST_ID] \
  [--generation-id GENERATION_ID] \
  [--wait] [--json]

harbor ingest status RUN_ID [--json]
harbor status RUN_ID [--json]
harbor ingest wait RUN_ID [--json]
harbor ingest watch RUN_ID [--refresh SECONDS]
harbor ingest pause RUN_ID [--json]
harbor ingest resume RUN_ID [--json]
harbor ingest cancel RUN_ID [--force] [--json]
harbor ingest retry RUN_ID --artifact ARTIFACT_ID [--artifact ARTIFACT_ID ...] [--json]
```

`start` generates omitted run, manifest, and generation IDs. `--wait` submits
the workflow and waits for its final result. `cancel` is graceful unless
`--force` is supplied.

### Live dashboard

`harbor ingest watch RUN_ID` launches a full-screen Textual control room. It
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
