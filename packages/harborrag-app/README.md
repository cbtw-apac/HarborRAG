# harborrag-app

Owns operator-facing API and CLI boundaries.

## Ingestion CLI

The CLI submits and controls the canonical Temporal workflow through
`AppService`; it does not construct connectors or repositories:

```bash
harborrag ingest start --tenant tenant-1 --connector local-docs
harborrag ingest start --tenant tenant-1 --connector local-docs --wait
harborrag ingest status RUN_ID --json
harborrag ingest watch RUN_ID
harborrag ingest wait RUN_ID
harborrag ingest pause RUN_ID
harborrag ingest resume RUN_ID
harborrag ingest retry RUN_ID --artifact ARTIFACT_ID
harborrag ingest cancel RUN_ID
```

Every one-shot command accepts `--json` after its action and returns
`{"ok":...,"data":...,"error":...}`. A failed operation exits with status 1.
Typer supplies grouped Rich help and shell completion. Rich renders one-shot
progress summaries, status colors, artifact lists, and the ingestion stage
sequence; concurrent artifact stages are shown as `in flight`.

`harborrag ingest watch RUN_ID` opens a Textual dashboard with live polling,
progress and stage panels, an attention queue, pause/resume controls, and
confirmed graceful cancellation. Use `harborrag --no-color ...` or the standard
`NO_COLOR` environment variable when one-shot terminal colors are undesirable;
`--json` never emits Rich formatting or spinner output.


## Package tests

Tests for this package live in:

```text
packages/harborrag-app/tests/
```

Run from the repository root:

```bash
pytest packages/harborrag-app/tests
```

Keep new tests in this folder when adding or changing behavior owned by this package.
