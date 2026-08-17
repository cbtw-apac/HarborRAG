# Known issues found by this suite

Findings from running the manual CLIs and live ingestions, most recently the
2026-08-17 real-data pass (Confluence space `AIF`, Jira project `AIF`, tenant
`ai-agent-factory`). Structural gates passed on all of it — these are the
defects found around the graph, not in it.

## Open

### 1. Chunking: `chunk[N] source location moved backward`

Two real Jira issues fail the CHUNK stage, non-retryable:

    ChunkValidationError: chunk[7] source location moved backward; chunk[8] source location moved backward

- Raised by `harborrag_engine/ingestion/chunking/pipeline/result.py:105` when
  emitted chunks are not monotonically ordered by source location.
- Live repro: ingest Jira project `AIF` — issues `AIF-15` and `AIF-28` fail
  every time (`document_release_chunk_invalid`, stage CHUNK). All other 163
  discovered documents chunk fine, so the trigger is specific issue content.
- Impact: the affected documents are silently absent from every projection.

### 2. Confluence attachment parser rejections

Two `AIF` attachments fail PARSE with `document_release_parser_rejected_document`
(retryable): `confluence://AIF/71369589/attachments/att71369610` and
`confluence://AIF/71369521/attachments/att71369542`. Likely unsupported or
corrupt attachment payloads; needs a look at what the parser rejected and
whether the format should be supported or skipped at admission instead of
counted as a failure.

### 3. Inconsistent terminal status for equivalent outcomes

For the same failure shape (2 documents failed, rest succeeded/skipped), the
Confluence run reported overall `FAILED` while the Jira run reported
`PARTIAL`. One of the two mappings is wrong; `PARTIAL` matches the documented
semantics.

### 4. Graph projection defects (found 2026-08-14, corpus-reproducible)

Documented when the suite was built; still open:

- Jira parent/subtask placeholders are double-keyed (numeric id vs issue key),
  so one issue can land as two nodes.
- Unresolved-relation placeholders share the real node's key; a real
  Confluence page flipped to `placeholder: true` on an all-Unchanged re-run.
  Deterministic repro in `unit/test_corpus.py` (placeholder assertions).
- Jira link types outside {blocks, duplicates} are flattened to `relates_to`
  and inward direction is lost.

The `ai-agent-factory` tenant carries 106 placeholders and is a good live
corpus for all three.

### 5. Stale release-gate reference

`tests/runtime_ingestion/smoke/README.md` points at
`.github/workflows/ingestion-release-gate.yml`, which does not exist in the
repo. Restore the workflow or delete the paragraph.

## Fixed on this branch (2026-08-17, runtime_ingestion smoke suite)

Both `tests/runtime_ingestion/smoke/` CLIs had been unrunnable since ~2026-08-03
(commit c5a4c7e7 era) and were repaired and verified live end-to-end:

- Hand-rolled `configuration_fingerprint` values could never match the
  worker's `connector_fingerprint()` equality check — inputs now use the real
  catalog fingerprint.
- `configured_sources_flow.py` looked connectors up by provider name
  ("confluence") instead of catalog name ("confluence-main"), and used the
  numeric locator as the sparse retrieval query (IDs do not appear in chunk
  content) — now resolves definitions by provider and queries by title.
- `projection_inspection.py` asserted the pre-v2 graph vocabulary
  (`has_section`/`has_table`) and referenced removed MinIO range-read fields —
  now asserts `contains` plus `section`/`table` node entity types.
