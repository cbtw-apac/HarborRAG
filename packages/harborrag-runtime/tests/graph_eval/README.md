# Graph evaluation suite

Contributor notes for the opt-in evaluation of the FalkorDB knowledge graph. It
complements the [ingestion smoke suite](../runtime_ingestion/smoke/README.md).

This directory holds two different things:

- **The CI test suite** (`unit/`): plain pytest that the `harborrag-runtime` entry of the
  `test-packages` job in `.github/workflows/test.yml` runs on pushes and pull requests.
  It needs no FalkorDB, credentials or manual steps, and it is the only part of this
  suite that gates a merge.
- **The manual CLIs** (`smoke/`): scripts you run against live stores at the moments
  listed below: FalkorDB for most, plus Postgres for `graph_health.py`, and Qdrant alone
  for `derived_vector_eval.py`. CI never runs them and pytest
  never collects them. They diagnose a running graph; they do not gate code.

Both use the same shared library, which belongs to neither:

```text
graph_eval/
├── corpus.py         # the deterministic corpus
├── sources/          # sample documents as per-source JSON fixtures
├── golden/           # case types and the gold-by-construction expectations
├── eval_metrics.py   # retrieval scoring
├── health/           # census gate logic, report diffing and committed baselines
├── unit/             # the CI test suite: everything pytest collects
└── smoke/            # the manual CLIs, plus the connection helpers they share
```

## Keep the committed baseline current

The corpus is deterministic, so its health report is too.
`health/baselines/graph-eval.json` is that report, committed, and
`unit/test_health_baseline.py` diffs every CI run against it.

When the baseline test fails, your change moved the projection output. If that was
intended (projector code, chunking config, a fixture or a schema bump), regenerate the
baseline, review the JSON diff (it shows the change's blast radius down to individual
node keys) and commit it in the same pull request:

```bash
HARBORRAG_UPDATE_BASELINE=1 .venv/bin/pytest \
  packages/harborrag-runtime/tests/graph_eval/unit/test_health_baseline.py
```

Never create a baseline proactively and never set that variable in CI: it rewrites the
baseline to whatever the run produced. The pull request that moves the baseline owns it,
and the diff in review is the sign-off.

## Run the manual CLIs

The scripts read connection settings from `env/.env.database`. Variables already set in
your shell win, so a stack on other ports needs only a prefix, for example
`FALKORDB_PORT=6390 POSTGRES_PORT=5442 .venv/bin/python ...`.

`HARBORRAG_EVAL_TENANT_ID` and `HARBORRAG_EVAL_GRAPH_NAME` override the eval tenant and
graph; their defaults, `graph-eval` and `harborrag-graph-eval`, match the committed
baseline. They steer the scripts that seed (`retrieval_eval.py` and
`gate_mutation_check.py`). `graph_health.py` reads whichever graph `--graph` names and
defaults to the one the runtime writes.

Every script exits **0** on pass, **1** on a gate or case failure and **2** when
prerequisites are unavailable. Exit 2 also covers a query failure after a successful
connection, such as Cypher that FalkorDB rejects, so read stderr instead of treating it as
a plain skip.

Human output (per-tenant and per-case summary lines, `PASS`/`FAIL` verdicts,
`GATE FAILURE` lines) goes to stderr. The JSON report goes to the file `--output` names,
and to stdout only when stdout is piped or redirected, so `| jq` and `> report.json` work
while interactive runs show only the summaries.

| CLI | Run it when | It answers |
| --- | --- | --- |
| `graph_health.py` | after a live ingest, deploy or re-run | Is the live graph structurally sound? |
| `graph_diff.py` | you hold two health reports from around a change | Did the build change graph identity or shape? |
| `retrieval_eval.py` | before merging search or projection changes | Does retrieval still meet the golden expectations? |
| `gate_mutation_check.py` | after changing any census in `graph_health.py` | Do the gates still detect what they claim to? |

Two live experiments, `derived_vector_eval.py` and `topology_index_eval.py`, follow the
same exit codes and are described at the end of this section.

### `graph_health.py`

Censuses the graph per tenant and fails on structural violations: unknown relation types
or node kinds, orphaned version-owned nodes, duplicate semantic relations, failed
constraints, missing merge-identity properties, empty or undiscoverable tenants, and
published document versions with no `DocumentVersion` node. Everything else is
report-only.

The publication gate reads the Postgres control plane from the same env file: a document
that fails after publication is absent from the graph, which no graph-side census can
see. That gate always runs, so Postgres must be reachable even when `--graph` points at
the seeded eval graph.

```bash
.venv/bin/python packages/harborrag-runtime/tests/graph_eval/smoke/graph_health.py
```

### `graph_diff.py`

The live-graph counterpart of the committed-baseline test: the same `diff_reports`, run on
reports captured around a real change.

```bash
SMOKE=packages/harborrag-runtime/tests/graph_eval/smoke
.venv/bin/python "$SMOKE/graph_health.py" --identities --output baseline.json
.venv/bin/python "$SMOKE/graph_health.py" --identities --output current.json
.venv/bin/python "$SMOKE/graph_diff.py" baseline.json current.json
```

Capture `baseline.json` before the change and `current.json` after it; `graph_diff.py` is
the exact-match gate. The defaults assert the deterministic-build property: Jaccard 1.0, no
new edge signatures and an unchanged `placeholder_count`. For intentional schema or corpus
changes, pass looser bounds or `--allow-new-signatures` and review the printed deltas.

Generate reports with `--identities`: diffing a report without it exits 1 rather than
passing a `null` Jaccard as green. To diff on censuses and signatures alone, opt out
explicitly with `--min-node-jaccard 0 --min-relation-jaccard 0`; a zero node bound also
turns off the placeholder-count gate. An empty identity list counts as missing, so a wiped
tenant fails instead of diffing clean against nothing.

The committed baseline uses the same report format, so `graph_diff.py` can compare
against it. Seed the eval graph first, then report on that graph rather than the
runtime's:

```bash
SMOKE=packages/harborrag-runtime/tests/graph_eval/smoke
.venv/bin/python "$SMOKE/retrieval_eval.py"
.venv/bin/python "$SMOKE/graph_health.py" --graph harborrag-graph-eval \
  --tenant graph-eval --identities --output current.json
.venv/bin/python "$SMOKE/graph_diff.py" \
  packages/harborrag-runtime/tests/graph_eval/health/baselines/graph-eval.json current.json
```

### `retrieval_eval.py`

Seeds the corpus into the isolated `harborrag-graph-eval` graph and checks the golden
expectations against `AuthoritativeGraphSearch`: path finding, subgraphs, triplets and
stale-version filtering. It needs FalkorDB only; Postgres is stubbed.

```bash
.venv/bin/python packages/harborrag-runtime/tests/graph_eval/smoke/retrieval_eval.py
```

### `gate_mutation_check.py`

Injects one violation per gated census and checks that every gate fires.
`GATE DID NOT FIRE` means the census misses what its gate claims to detect; fix the
census. Cleanup empties the eval tenant, and `retrieval_eval.py` reseeds it.

```bash
.venv/bin/python packages/harborrag-runtime/tests/graph_eval/smoke/gate_mutation_check.py
```

### `derived_vector_eval.py` and `topology_index_eval.py`

`derived_vector_eval.py` needs a live Qdrant. It publishes one contextual point under an
isolated collection prefix, deletes it, repairs it from the artifact, corrupts its payload,
repairs it again and always deletes the temporary collection.

`topology_index_eval.py` needs a live FalkorDB and writes only tenant-hashed graphs under
`hr_topology_eval_*`. It checks structural-node enrichment, entity descriptions,
consolidated typed edges, bottom-up parent summaries, corruption detection, repair and
tenant isolation.

```bash
.venv/bin/python packages/harborrag-runtime/tests/graph_eval/smoke/derived_vector_eval.py
.venv/bin/python packages/harborrag-runtime/tests/graph_eval/smoke/topology_index_eval.py
```

## Add a sample document

Samples are data, not code: one directory per source type under `sources/fixtures/`, one
JSON file per document, and a `_defaults.json` per directory for shared keys (keys in a
document's file win). Adding a sample is adding a file; adding a source type is adding a
directory.

The CI test suite enforces three rules:

- Every sample needs a `note` saying which projection shape it exercises.
- The corpus must emit `CORPUS_SIGNATURES` exactly; a new edge signature is a reviewed
  vocabulary change, not a drop-in.
- Every document must yield chunks, a version node and exactly one source item.

The `local` documents `runbook`, `architecture`, `decisions` and `incident`, and every
document a golden case names, are pinned: changing their IDs, titles, elements or
relations moves the golden expectations with them.
