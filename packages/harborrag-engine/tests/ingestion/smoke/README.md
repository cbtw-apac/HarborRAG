# Real indexing smoke check

This standalone script runs the completed engine indexing boundary through a
real embedding provider, Qdrant, and FalkorDB. It does not use pytest, mocks,
recorded responses, or fake provider clients. It stages the same generation
twice, validates deterministic identities and inactive state, then removes all
probe data.

Run it only against disposable services and a least-privilege embedding
credential. The check makes two small embedding requests and may consume paid
quota.

## Prerequisites

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

## Run

From the repository root:

```bash
HARBOR_SMOKE_ENV_FILE=/secure/path/indexing-smoke.env \
  .venv/bin/python \
  packages/harborrag-engine/tests/ingestion/smoke/indexing.py
```

The script prints only provider-independent identities and counts. It never
prints chunk text, embedding vectors, credentials, or raw provider payloads.

## Exit codes

| Code | Meaning |
| --- | --- |
| `0` | Both stores passed validation and probe data was removed. |
| `1` | Configuration existed, but a real operation, invariant, or cleanup failed. |
| `2` | A required dependency or embedding setting is unavailable. |
