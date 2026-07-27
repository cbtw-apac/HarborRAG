# harborrag-app

Owns operator-facing API and CLI boundaries.

## Ingestion CLI

The CLI submits and controls the canonical Temporal workflow through
`AppService`; it does not construct connectors or repositories:

```bash
harborrag ingest start --tenant tenant-1 --connector local-docs
harborrag ingest start --tenant tenant-1 --connector jira --limit 3 --wait
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

## Retrieval CLI

The retrieval command embeds a tenant-scoped query, searches active Qdrant
vectors, expands matching chunk context through FalkorDB, and applies
reciprocal-rank fusion:

```bash
harborrag retrieve "deployment requirements" --tenant tenant-1 --top-k 5
harborrag retrieve "deployment requirements" --tenant tenant-1 --top-k 5 --json
```

Retrieved text is omitted by default so shell logs and automation artifacts do
not collect source content. Pass `--include-content` only when the caller is
authorized to receive document text. Unexpected provider exception messages
are also suppressed at the application boundary; the public envelope retains
only the stable exception type.


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
