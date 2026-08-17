# Graph evaluation suite

Opt-in evaluation of the FalkorDB knowledge graph, complementing the ingestion
smoke suite (`../../runtime_ingestion/smoke/`).

This directory holds **two different things** — do not confuse them:

- **The CI test suite** (`unit/`): plain pytest, collected automatically on
  every push/PR by the `harborrag-runtime` job in `test.yml`. No FalkorDB, no
  credentials, no manual steps. This is the only part of this suite that gates
  a merge.
- **The manual CLIs** (`smoke/`): operational scripts a human runs against a
  **live** FalkorDB at specific moments (see the table below). CI never runs
  them and pytest never collects them. They diagnose a running graph; they do
  not gate code.

Both draw on the same shared library, which belongs to neither:

    corpus.py             the deterministic corpus
    sources/              sample documents as per-source JSON fixtures
    golden/               case types and the gold-by-construction expectations
    eval_metrics.py       retrieval scoring
    health/               census gate logic, report diffing, committed baselines
    unit/                 the CI test suite -- everything pytest collects
    smoke/                the manual CLIs, plus the client they share

## The CI test suite (`unit/`)

Runs on every push/PR; nothing to remember. Its centerpiece is the committed
baseline: the corpus is deterministic, so its health report is too —
`health/baselines/graph-eval.json` is that report, committed, and
`unit/test_health_baseline.py` diffs every CI run against it.

**When the baseline test fails**, your change moved the projection output.
If that was intended (projector code, chunking config, a fixture, a schema
bump), regenerate and review the JSON diff — it is the change's blast radius,
down to individual node keys — then commit it in the same PR:

    HARBORRAG_UPDATE_BASELINE=1 .venv/bin/pytest \
        packages/harborrag-runtime/tests/graph_eval/unit/test_health_baseline.py

Never create a baseline proactively and never set that variable in CI — it
rewrites the baseline to whatever the run produced. The baseline is owned by
whoever's PR moves it; the diff in review is the sign-off.

## The manual CLIs (`smoke/`)

All need a running FalkorDB (env in `env/.env.database`) and share one
exit-code convention: **0** pass, **1** gate/case failure, **2** prerequisites
unavailable. Exit 2 also covers a query failure *after* a successful connect
(e.g. Cypher FalkorDB rejects), so read stderr instead of treating it as a
plain skip.

| CLI | Run it when | It answers |
|---|---|---|
| `graph_health.py` | after a live ingest, deploy, or re-run | is the live graph structurally sound? |
| `graph_diff.py` | you hold two health reports from around a change | did the build change graph identity or shape? |
| `retrieval_eval.py` | before merging search or projection changes | does retrieval still meet the golden expectations? |
| `gate_mutation_check.py` | after changing any census in `graph_health.py` | do the gates still detect what they claim to? |

**`graph_health.py`** censuses the graph per tenant and hard-fails on
structural violations (unknown relation types or node kinds, orphaned
version-owned nodes, duplicate semantic relations, failed constraints, missing
merge-identity properties, empty/undiscoverable tenants). Everything else is
report-only.

    .venv/bin/python packages/harborrag-runtime/tests/graph_eval/smoke/graph_health.py

**`graph_diff.py`** is the live-graph counterpart of the committed-baseline
test — same `diff_reports`, but on reports a human captured around a real
change:

    graph_health.py --identities --output baseline.json   # before a change
    graph_health.py --identities --output current.json    # after
    graph_diff.py baseline.json current.json              # exact-match gate

Defaults assert the deterministic-build property: Jaccard 1.0, no new edge
signatures, unchanged `placeholder_count`. For intentional schema/corpus
changes pass looser bounds and/or `--allow-new-signatures` and review the
printed deltas. Generate reports with `--identities` — diffing a report
without it exits 1 rather than passing a `null` Jaccard as green. To diff on
censuses and signatures alone, opt out explicitly with `--min-node-jaccard 0
--min-relation-jaccard 0` (a zero node bound also disables the
placeholder-count gate). The committed baseline is written in the same report
format, so `graph_diff.py` can be pointed straight at it.

**`retrieval_eval.py`** seeds the corpus into the isolated
`harborrag-graph-eval` graph and asserts the golden expectations against
AuthoritativeGraphSearch (path finding, subgraphs, triplets, stale-version
filtering). Needs FalkorDB only; Postgres is stubbed.

    .venv/bin/python packages/harborrag-runtime/tests/graph_eval/smoke/retrieval_eval.py

**`gate_mutation_check.py`** injects one violation per gated census and
asserts every gate fires. `GATE DID NOT FIRE` means the census misses what its
gate claims to detect — fix the census. Cleanup empties the eval tenant;
`retrieval_eval.py` reseeds.

    .venv/bin/python packages/harborrag-runtime/tests/graph_eval/smoke/gate_mutation_check.py

## Adding a sample document

Samples are data, not code: one directory per source type under
`sources/fixtures/`, one JSON file per document, `_defaults.json` per directory
for shared keys (file keys win). Adding a sample is adding a file; adding a
source type is adding a directory.

Three rails, all enforced by the CI test suite:

- every sample needs a `note` saying which projection shape it exercises;
- the corpus must emit `CORPUS_SIGNATURES` *exactly* — a new edge signature is
  a reviewed vocabulary change, not a drop-in;
- every document must yield chunks, a version node, and exactly one source item.

The four `local` documents and every document named by a golden case are
pinned: changing their ids, titles, elements, or relations moves the golden
expectations with them.
