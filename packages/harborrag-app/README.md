# harborrag-app

Owns operator-facing API and CLI boundaries.

## Ingestion CLI

The CLI submits and controls the canonical Temporal workflow through
`AppService`; it does not construct connectors or repositories:

```bash
harbor ingest start --tenant tenant-1 --connector local-docs
harbor ingest start --tenant tenant-1 --connector local-docs --wait
harbor ingest status RUN_ID --json
harbor ingest watch RUN_ID
harbor ingest wait RUN_ID
harbor ingest pause RUN_ID
harbor ingest resume RUN_ID
harbor ingest retry RUN_ID --artifact ARTIFACT_ID
harbor ingest cancel RUN_ID
```

Every one-shot command accepts `--json` after its action and returns
`{"ok":...,"data":...,"error":...}`. A failed operation exits with status 1.
Typer supplies grouped Rich help and shell completion. Rich renders one-shot
progress summaries, status colors, artifact lists, and the ingestion stage
sequence; concurrent artifact stages are shown as `in flight`.

`harbor ingest watch RUN_ID` opens a Textual dashboard with live polling,
progress and stage panels, an attention queue, pause/resume controls, and
confirmed graceful cancellation. Use `harbor --no-color ...` or the standard
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
