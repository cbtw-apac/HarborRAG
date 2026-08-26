# Ingestion smoke test

This opt-in check exercises the deployed ingestion path:

```text
local discovery
  -> Temporal source/batch/document workflows
  -> immutable MinIO artifacts
  -> Postgres publication
  -> Qdrant route/evidence projections
  -> FalkorDB structural/link graph
  -> dense, sparse, and hybrid authoritative retrieval
```

Start the data and Temporal stacks with the fixture directory mounted into the
worker:

```bash
HARBORRAG_LOCAL_SOURCE_DIR="$PWD/packages/harborrag-runtime/tests/runtime_ingestion/smoke/fixtures" \
docker compose \
  --env-file env/.env.database \
  --env-file env/.env.temporal \
  -f deploy/compose/docker-compose.temporal.yml \
  --profile worker up -d --build
```

Then run:

```bash
.venv/bin/python \
  packages/harborrag-runtime/tests/runtime_ingestion/smoke/ingestion_flow.py
```

The runner loads the ignored database, Temporal, and model env files without
printing secret values. It publishes the two Markdown fixtures and verifies:

- immutable raw, canonical, chunk, representation, and projection artifacts;
- Qdrant dense, sparse, and hybrid retrieval with active-version validation;
- chunk payload content, citation fields, and complete chunk provenance;
- FalkorDB forward/reverse traversal, endpoint integrity, and no duplicate
  semantic relations;
- unchanged replay without parsing, chunking, or encoding;
- Redis loss without publication-authority loss;
- connector-free reindexing with unchanged graph topology;
- bounded Temporal history and successful workflow completion.

The smoke test deliberately calls `FLUSHALL` on the configured Redis database.
Run it only against the disposable ingestion cache, never a shared Redis
deployment.

To inspect bounded live Confluence and Jira samples using the ignored connector
credentials, run:

```bash
.venv/bin/python \
  packages/harborrag-runtime/tests/runtime_ingestion/smoke/configured_sources_flow.py
```

This live check prints counts, hashes, locators, and structural observations;
it does not print source content or secret values.

The manual release gate is
`.github/workflows/ingestion-release-gate.yml`. It requires a self-hosted runner
next to an already deployed worker, an exact deployed commit SHA, the protected
`ingestion-release` environment, and explicit confirmation that Redis is
disposable. It has no push, tag, release, or package-publishing trigger.
