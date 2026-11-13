# Ingestion smoke test

Contributor notes for the opt-in check of the deployed ingestion path. `ingestion_flow.py`
publishes the two Markdown files in `fixtures/` through the running stack and verifies
every step:

```text
local discovery
  -> Temporal source/batch/document workflows
  -> immutable MinIO artifacts
  -> Postgres publication
  -> Qdrant route/evidence projections
  -> FalkorDB structural/link graph
  -> dense, sparse, and hybrid authoritative retrieval
```

The run calls `FLUSHALL` on the configured Redis database. Run it only against a
disposable ingestion cache, never a shared Redis deployment.

## Start the stack with the fixtures mounted

On a fresh checkout, first create the `env/` files with
`scripts/deployment/dev.sh bootstrap`, as in the
[Quick Start](../../../../../docs/getting-started/quick-start.md). Bootstrap also
generates `HARBORRAG_SECRETS_ENCRYPTION_KEY` in `env/.env.database` while no database
volume exists; the worker started below cannot use the control database without it.

1. Start the data services (PostgreSQL, Qdrant, FalkorDB, Redis and MinIO). This also
   creates the `harborrag-data-network` network that the Temporal stack joins:

   ```bash
   scripts/deployment/dev.sh data
   ```

2. Start Temporal and the ingestion worker, with the fixture directory mounted as the
   worker's local source folder:

   ```bash
   HARBORRAG_LOCAL_SOURCE_DIR="$PWD/packages/harborrag-runtime/tests/runtime_ingestion/smoke/fixtures" \
   docker compose \
     --env-file env/.env.database \
     --env-file env/.env.temporal \
     -f deploy/compose/docker-compose.temporal.yml \
     --profile worker up -d --build
   ```

   The compose file expects the external `harborrag-model-cache` volume, which
   `scripts/deployment/dev.sh worker` and `up` create. On a machine where neither has run,
   create it first with `docker volume create harborrag-model-cache`.

## Run the check

```bash
.venv/bin/python \
  packages/harborrag-runtime/tests/runtime_ingestion/smoke/ingestion_flow.py
```

The runner loads the git-ignored `env/.env.database`, `env/.env.temporal`,
`env/.env.models` and `env/.env.connector` without printing secret values, and verifies:

- immutable raw, canonical, chunk, representation and projection artifacts;
- Qdrant dense, sparse and hybrid retrieval with active-version validation;
- chunk payload content, citation fields and complete chunk provenance;
- FalkorDB forward and reverse traversal, endpoint integrity and no duplicate semantic
  relations;
- an unchanged replay without parsing, chunking or encoding;
- Redis loss without loss of publication authority;
- connector-free reindexing with unchanged graph topology;
- bounded Temporal history and successful workflow completion.

## Inspect live Confluence and Jira samples

`configured_sources_flow.py` runs bounded live Confluence and Jira ingestion with the
connector credentials in the ignored env files:

```bash
.venv/bin/python \
  packages/harborrag-runtime/tests/runtime_ingestion/smoke/configured_sources_flow.py
```

It prints counts, hashes, locators and structural observations, never source content or
secret values.
