# Real ingestion smoke checks

Two standalone scripts live here. `chunking.py` runs the connector → parser →
normalizer → chunking path and reports what the chunker did; `indexing.py` runs
the indexing boundary against real stores. Neither uses pytest, mocks,
recorded responses, or fake provider clients. Read the shared smoke-test safety
and exit-code guidance in `packages/harborrag-adapters/tests/README.md`
("Real-system smoke tests") before using real credentials or content.

## Real chunking smoke check

`chunking.py` chunks real connector documents end to end and prints the vector
points that revision would write to Qdrant. It needs no databases, no embedding
provider, and no paid quota — only a configured connector and the parser
dependencies.

The JSON document holds one thing: vector-store data. Parser, routing, and
chunking diagnostics are progress output on stderr, so stdout can be diffed
against what a collection actually contains.

The pipeline is the production one: connectors and parsers come from
`config/connectors.yaml` and `config/parsers.yaml` (falling back to their
`.example.yaml` templates), and the chunking service is composed exactly as
`build_ingestion_dependencies` composes it — the runtime
`ApproximateTokenCounter` plus the recursive refiner and the optional
markdown/HTML/JSON structure splitters. Chunk shapes are entirely
token-count-driven, so a different counter would report chunking that
production does not perform. Each document is chunked twice from the same
normalized document to prove the manifest fingerprint is deterministic.

### Prerequisites

Run from the repository root with Python 3.12 and install the parser
dependencies (add `--extra pdf` for PDF and OCR engines):

```bash
uv sync --package harborrag-adapters --extra parsers --extra pdf
```

Credentials come from `env/.env.connector` and `env/.env.parser`, or from one
file named by `HARBOR_SMOKE_ENV_FILE`. Exported variables always win. Copy the
templates before the first run:

```bash
cp config/connectors.example.yaml config/connectors.yaml
cp env-example/.env.connector.example env/.env.connector
```

`local` is the only connector needing no credentials — set `LOCAL_SOURCE_PATH`
(relative paths resolve from the repository root). Connector-specific variables
are documented in `packages/harborrag-adapters/tests/connectors/smoke/README.md`.

Unlike the connector smoke scripts, this check uses only the declarative parser
catalog, so plain image files are not routed to RapidOCR and will not yield
text.

### Run

```bash
LOCAL_SOURCE_PATH=./docs python \
  packages/harborrag-engine/tests/ingestion/smoke/chunking.py

python packages/harborrag-engine/tests/ingestion/smoke/chunking.py \
  --connector confluence --limit 3 --output json

python packages/harborrag-engine/tests/ingestion/smoke/chunking.py \
  --connector jira --limit 5 --profile jira
```

| Option | Meaning |
| --- | --- |
| `--connector` | Configured connector name (default: `local`) |
| `--limit` | Max records to discover and chunk (default: `3`) |
| `--profile` | Force one profile instead of letting the router select it |
| `--output json` | Save one report file per record (default: save nothing) |
| `--output-dir` | Directory for saved reports (default: `tests/ingestion/smoke/output`) |
| `--include-content` | Embed real chunk text in the report (off by default) |

`--profile` is the cheap way to exercise a strategy the router would not reach
for the available documents. Forcing a profile that does not fit the input is
expected to fail: `--profile json` on Markdown reports
`JSON chunk requires json_path` and exits `1`.

### Saved output

By default nothing is written to disk. `--output json` saves one file per
discovered record — one Confluence page, JIRA issue, or local file per file —
named `<provider>-<sanitized-record-id>.json`:

```text
output/confluence-confluence_HARBORRAG_95453254.json
output/local-file_home_you_docs_report.md.json
```

Each file repeats the `vector` run header before that record's own points, so a
single page's file can be read, diffed, or attached on its own. Saved files are
gitignored.

### Output

The JSON document goes to stdout so it stays parseable; every progress line and
every check result goes to stderr.

```json
{
  "smoke": "ingestion-chunking-vector",
  "vector": {
    "collection": "harborrag_chunks",
    "generation_id": "smoke-generation",
    "distance": "cosine",
    "dimension": 1536,
    "metadata_indexes": ["generation_id", "logical_chunk_id", "..."],
    "payload_includes_content": false,
    "vectors_computed": false,
    "embedding": {
      "model": "text-embedding-3-small",
      "identity_source": "runtime settings",
      "configuration_fingerprint": "5084...",
      "context_maximum_characters": 512,
      "text_rendering_version": "1",
      "maximum_batch_tokens": 8192
    }
  },
  "documents": [
    {
      "record_id": "confluence://HARBORRAG/95518771",
      "points": [
        {
          "action": "UPSERT",
          "id": "vector:6cfd...",
          "tenant_id": "smoke-chunking",
          "vector": null,
          "embedding_input": {"context_header": "...", "characters": 4531, "token_count": 1140},
          "payload": {"...": "VectorPayloadBuilder output"}
        }
      ]
    }
  ],
  "totals": {"documents": 1, "failed_documents": 0, "points": 3, "embedding_tokens": 3377},
  "status": "passed"
}
```

The points are not reconstructed here. `IncrementalChunkDiffer` and
`VectorMutationPlanner` produce them, so the actions, payload shape, and every
content-derived field are production's own. A probe revision has no active
manifest, so every chunk classifies as new and plans one `UPSERT`; placeholder
zero vectors are fed to the planner purely to satisfy its contract and are never
reported (`vector` is always `null`, `vectors_computed` is always `false`).

**The point ids are not the ids production would write.** A point id is a
`sha256` over `(tenant_id, collection, generation_id, chunk_revision_id,
embedding_configuration_fingerprint)`, and two of those five are stand-ins:

| Field | Here | In production |
| --- | --- | --- |
| `tenant_id` | `smoke-chunking` | The real tenant |
| `generation_id` | `smoke-generation` | Minted by the ingestion run |

Everything else in the payload is real: the chunk and artifact identities,
`content_hash`, `token_count`, `structural_path`, ranges, `source_kind`,
`chunk_role`, `content_reference`, `index_state`, and `is_active`.

The fifth input needs one setting. `embedding.identity_source` names where the
model and dimensions came from: `runtime settings`
(`HARBORRAG_EMBEDDING_MODEL` plus `HARBORRAG_EMBEDDING_DIMENSIONS`), the model
catalog path, or `placeholder` when embedding is not configured at all. Those two
values decide `embedding_configuration_fingerprint`, which is stored in every
payload — so with `placeholder`, that field is keyed to a stand-in model too:

```bash
HARBORRAG_EMBEDDING_MODEL=text-embedding-3-small \
HARBORRAG_EMBEDDING_DIMENSIONS=1536 \
  python packages/harborrag-engine/tests/ingestion/smoke/chunking.py \
    --connector confluence --output json
```

### What gets embedded, and where chunk text lives

Each point's `embedding_input` is the exact text whose vector would be stored
against it, rendered by the indexing stage's own `EmbeddingInputPreparer`:

```text
Title: <document title>
Section: <a > b > c>          # only when the chunk has a section path
Role: <chunk role>
Issue/Page/File/Symbol: ...   # whichever metadata the source provided
                              # bounded to context_maximum_characters
<chunk content>
```

`context_header`, `characters`, and `token_count` are always reported; the full
`text` (header plus content) needs `--include-content`, because it is source
content. `totals.embedding_tokens` therefore exceeds the chunks' own token count
by exactly the header cost.

Note that `ChunkRecord.embedding_text` — built at chunking time from
`Document:`/`Section:` lines — is **not** what the vector path embeds. The
indexing stage re-renders its own header, so the two differ by design.

Chunk text is stored, just not by the vector store:

| Store | Holds |
| --- | --- |
| Object store (`ChunkPersistenceService` → `chunks/<chunk_revision_id>.json`) | The chunk bodies and manifest |
| Qdrant payload | Identities, `content_hash`, `token_count`, `structural_path`, page/line ranges, and a `content_reference` back to the body |

`content_reference` is the logical URN `harborrag:chunk:<chunk_revision_id>`.
Persistence does not write a `body_uri` back onto manifest references, so
production stores this same fallback form; it resolves by convention through the
chunk object repository rather than as a direct object path.

`IndexingConfig.include_chunk_content_in_vector_payload` defaults to `False`, so
Qdrant carries the reference rather than the text. The report echoes that value
under `vector.payload_includes_content`; when it is `True`, the payload gains a
`content` field and the saved file then contains real source text regardless of
`--include-content`.

The worker builds `IndexingConfig` with identities only, so every preparation
knob this check reports is the production default.

### Success criteria

A document passes when chunking returns without a validation error and every
check passes: chunks were produced, the manifest is valid, no chunk exceeds the
profile's hard maximum, ordinals are contiguous, content hashes match their
content, chunks form one ordered chain, manifest counts and fingerprint agree
with the records, the repeated run reproduced the fingerprint, and every chunk
rendered one embedding input within `maximum_batch_tokens`.

The checks are printed to stderr, not stored in the document — they describe the
run, not the collection. A failing check names itself:

```text
[chunking] check failed [token_limits]: ordinals over 1100 tokens: [4]
[chunking] failed confluence://HARBORRAG/95518771: points=5 strategy=confluence ...
```

A document that fails a check still reports its points: the points are what the
store would receive either way.

### Exit codes

| Code | Meaning |
| --- | --- |
| `0` | Every discovered document chunked and passed all checks |
| `1` | Discovery, a stage, or a check failed for at least one document |
| `2` | The connector or parser catalog is unconfigured, or a dependency is missing |

- Exit `2`: the printed message names the missing connector or variable; check
  `config/connectors.yaml` and `env/.env.connector`.
- No records: confirm the source contains readable documents matching the
  configured `allowed_extensions` and scoping, and review the skip lines on
  stderr.
- Empty `structural_path` on every payload: the parser returned the document as
  one element, so the chunker had no headings to segment on and split on tokens
  alone. The stderr line names the strategy that ran.

## Real indexing smoke check

This standalone script runs the completed engine indexing boundary through a
real embedding provider, Qdrant, and FalkorDB. It does not use pytest, mocks,
recorded responses, or fake provider clients. It stages the same generation
twice, validates deterministic identities and inactive state, then removes all
probe data.

Run it only against disposable services and a least-privilege embedding
credential. The check makes two small embedding requests and may consume paid
quota.

### Indexing prerequisites

Install the real clients:

```bash
uv pip install -e "packages/harborrag-adapters[llm,qdrant,falkordb]"
```

Start the local database stack if Qdrant and FalkorDB are not already running:

```bash
cp env-example/.env.database.example env/.env.database
export DATABASE_ENV_FILE=env/.env.database
scripts/deployment/database_up.sh
```

The smoke dotenv file must combine the database settings with a real embedding
deployment. Exported variables take precedence, so it is also safe to load the
database file and export embedding credentials separately.

Required embedding variables:

```text
HARBOR_EMBED_PROVIDER
HARBOR_EMBED_MODEL
HARBOR_EMBED_EXPECTED_DIMENSIONS
```

Most hosted providers also require `HARBOR_EMBED_API_KEY`. Provider
options use the existing `HARBOR_EMBED_*` names documented in
`env-example/.env.models.example`. Set
`HARBOR_EMBED_CONFIGURABLE_DIMENSIONS=true` only when the selected
provider supports an explicit dimensions parameter.

Database defaults match `deploy/compose/docker-compose.database.yml`:

```text
Qdrant:  http://127.0.0.1:6333
FalkorDB: 127.0.0.1:6379
```

Relevant overrides are `HARBOR_SMOKE_QDRANT_URL`,
`HARBOR_SMOKE_QDRANT_API_KEY`, `HARBOR_SMOKE_QDRANT_PREFER_GRPC`,
`HARBOR_SMOKE_QDRANT_PREFIX`, `FALKORDB_HOST`, `FALKORDB_PORT`,
`FALKORDB_USERNAME`, `FALKORDB_PASSWORD`, and `FALKORDB_SSL`.

### Indexing run

From the repository root:

```bash
HARBOR_SMOKE_ENV_FILE=/secure/path/indexing-smoke.env \
  .venv/bin/python \
  packages/harborrag-engine/tests/ingestion/smoke/indexing.py
```

The script prints only provider-independent identities and counts. It never
prints chunk text, embedding vectors, credentials, or raw provider payloads.

### Local indexing performance gate

The integration suite can stage a deterministic 128-record batch directly
through both local databases without calling a paid embedding provider:

```bash
HARBORRAG_QDRANT_INTEGRATION=1 \
HARBORRAG_FALKORDB_INTEGRATION=1 \
  pytest \
    packages/harborrag-engine/tests/ingestion/integration/test_qdrant_vector_indexing.py \
    packages/harborrag-engine/tests/ingestion/integration/test_falkordb_graph_indexing.py \
    -m integration
```

Set `HARBORRAG_INDEXING_PERFORMANCE_RECORDS` to change the batch size and
`HARBORRAG_INDEXING_MAX_SECONDS` to set the environment-specific upper bound.
The tests validate stored vector and graph identities after timing the stage,
then remove their uniquely named probe collection and graph records.

### Indexing exit codes

| Code | Meaning |
| --- | --- |
| `0` | Both stores passed validation and probe data was removed. |
| `1` | Configuration existed, but a real operation, invariant, or cleanup failed. |
| `2` | A required dependency or embedding setting is unavailable. |
