# harborrag-app

Owns operator-facing API and CLI boundaries.

## Ingestion CLI

The CLI submits and controls the canonical Temporal workflow through
`AppService`; it does not construct connectors or repositories:

```bash
harborrag ingest start --tenant tenant-1 --connector-id harborrag-workspace
harborrag ingest start --tenant tenant-1 --connector-id jira-main --limit 3 --wait
harborrag ingest start --tenant tenant-1 --connector-id harborrag-workspace --wait
harborrag ingest status RUN_ID --json
harborrag ingest watch RUN_ID
harborrag ingest wait RUN_ID
harborrag ingest pause RUN_ID
harborrag ingest resume RUN_ID
harborrag ingest cancel RUN_ID
```

Starting a run resolves the named connector into the source-ingestion contract,
persists a `PENDING` task in Postgres, and submits
`harborrag.source_ingestion`. Re-running the same source under a new run ID
replays from the earliest reusable immutable artifact.

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
harborrag retrieve "deployment requirements" --tenant tenant-1 --lane sparse --no-graph
harborrag retrieve "deployment requirements" --tenant tenant-1 --filters-json '{"category":"runbook"}'
```

Retrieved text is omitted by default so shell logs and automation artifacts do
not collect source content. Pass `--include-content` only when the caller is
authorized to receive document text. Unexpected provider exception messages
are also suppressed at the application boundary; the public envelope retains
only the stable exception type.

## Retrieval API

The authenticated API exposes the same runtime retrieval facade at
`POST /v1/retrieval/vector`:

```json
{
  "query": "deployment requirements",
  "top_k": 5,
  "lane": "hybrid",
  "filters": {"category": "runbook"},
  "observe_graph": true,
  "score_threshold": 0.25,
  "include_content": true,
  "include_metadata": true
}
```

The caller must have at least the `reader` role. Tenant scope is explicit and
cannot be overridden by a request filter. The response contains a request ID,
selected lane, ranked results, and retrieval diagnostics.

Graph retrieval uses separate typed POST operations:

- `/v1/retrieval/graph/triplets`
- `/v1/retrieval/graph/paths`
- `/v1/retrieval/graph/subgraphs`

Search is not duplicated as GET because query text and nested filters should
remain in the request body rather than URLs, access logs, and intermediary
caches. The former `/v1/retrieval/search` route has been removed.


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
