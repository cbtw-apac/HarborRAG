# Runtime Settings Overrides — Acceptance Test Guide (AC1–AC11)

How to test the runtime settings overrides feature: AC1–AC6
(`setting_overrides`, `setting_scope_revisions`, `/v1/admin/settings*`), AC8
(overrides applied to real ingestion), AC9 (Temporal activity options), AC10
(a running run keeps its settings), AC11 (database unavailable).

Each AC can be tested two ways:

- **pytest** — automated, fast, deterministic. Preferred.
- **Postman / curl** — manual, against a running stack. Use when you need to
  see the real end-to-end behavior (Temporal, Postgres, MinIO).

---

## 0. Setup

### pytest

No running stack required — uses fakes and a Postgres test DB.

```bash
# AC1 + AC2
uv run pytest packages/harborrag-app/tests/test_api_admin_settings.py -k "ac1 or ac2" -v

# AC3
uv run pytest packages/harborrag-app/tests/test_api_admin_settings.py -k ac3 -v

# AC4
uv run pytest packages/harborrag-app/tests/test_api_admin_settings.py -k ac4 -v

# AC5
uv run pytest packages/harborrag-app/tests/test_api_admin_settings.py -k ac5 -v

# AC6
uv run pytest packages/harborrag-runtime/tests/unit/test_settings_seed.py -v

# AC8
uv run pytest packages/harborrag-app/tests/test_connection_service.py -k ac8 -v
uv run pytest packages/harborrag-adapters/tests/parsers/unit/test_parser_family_architecture.py -v
uv run pytest packages/harborrag-adapters/tests/parsers/unit/pdf/test_parser.py -v
uv run pytest packages/harborrag-engine/tests/ingestion/unit/test_plan_for_max_tokens.py -v
uv run pytest packages/harborrag-runtime/tests/runtime_ingestion/unit/source/test_plan.py -v
uv run pytest packages/harborrag-runtime/tests/runtime_ingestion/unit/temporal/test_submission.py -v

# AC9 + AC10
uv run pytest packages/harborrag-app/tests/test_connection_service.py -k "ac9 or ac10" -v
uv run pytest packages/harborrag-runtime/tests/runtime_ingestion/unit/temporal/test_client.py -k "overlay or deployment_defaults" -v

# AC9 part 2 — GET /v1/admin/failures
uv run pytest packages/harborrag-adapters/tests/repositories/unit/test_ingestion_control_reliability.py -k list_failures -v
uv run pytest packages/harborrag-app/tests/test_failures_service.py -v
uv run pytest packages/harborrag-app/tests/test_export_openapi.py -v
```

Whole suite in one go:

```bash
uv run pytest \
  packages/harborrag-runtime/tests/unit/test_settings_seed.py \
  packages/harborrag-app/tests/test_settings_service.py \
  packages/harborrag-app/tests/test_api_admin_settings.py \
  packages/harborrag-app/tests/test_api_settings_seed.py \
  packages/harborrag-adapters/tests/repositories/integration/test_setting_scope_revision_repository.py \
  packages/harborrag-adapters/tests/repositories/integration/test_setting_override_repository.py \
  -q
```

### Postman / curl

Requirements:

- A running stack: `./scripts/deployment/dev.sh up` (or
  `./scripts/deployment/dev.sh api --build` after an API code change). API
  listens on `http://localhost:8000` (`HARBORRAG_API_PORT` in
  `env/.env.api`).
- **A dedicated test tenant**, isolated from `DEFAULT` data. A tenant here
  is not a registered entity — it's just the `tenants` claim in the JWT
  (`packages/harborrag-app/src/harborrag_app/api/auth/tenancy.py:18-50`).
  Minting a token with `tenants: ["qa"]` is enough. Everything below uses
  tenant `qa`.

#### Mint tokens (admin / editor / reader, tenant `qa`)

Lifetime must be ≤ `HARBORRAG_AUTH_MAX_TOKEN_LIFETIME_SECONDS` (server
default 3600s/1h if unset in `env/.env.api`; a longer token gets
`401 invalid token lifetime`).

```bash
cat > /tmp/mint_tokens.py <<'PY'
import datetime
import jwt

SECRET = "<paste HARBORRAG_AUTH_SECRET from env/.env.api>"


def token(role: str, tenants: list[str]) -> str:
    now = datetime.datetime.now(datetime.timezone.utc)
    return jwt.encode(
        {
            "sub": "tester",
            "role": role,
            "tenants": tenants,
            "iat": now - datetime.timedelta(seconds=1),
            "exp": now + datetime.timedelta(minutes=59),
            "iss": "harborrag",
            "aud": "harborrag-api",
        },
        SECRET,
        algorithm="HS256",
    )


for role in ("admin", "editor", "reader"):
    print(f"{role.upper()}={token(role, ['qa'])}")
PY
uv run python /tmp/mint_tokens.py
```

In Postman: store as Collection Variables `admin_token`/`editor_token`/
`reader_token`; set each request's Authorization to Bearer Token
`{{admin_token}}` (or the matching role).

#### Build the fixture (2 projects + 2 connections, via the real API — no SQL)

**1) Project `qa-project`**

- `PUT http://localhost:8000/v1/admin/projects/qa-project`
- Auth: `admin_token`
- Body: `{ "project_name": "QA Project" }`
- Expected: `201`

**2) Project `qa-project-2`** (used by AC2's "different project" case)

- `PUT http://localhost:8000/v1/admin/projects/qa-project-2`
- Auth: `admin_token`
- Body: `{ "project_name": "QA Project 2" }`
- Expected: `201`

**3) Connection `qa-conn-a`** in `qa-project` (`local_file`, no credentials;
`source_path` must be inside the mounted `LOCAL_SOURCE_PATH`, default
`/data/sources`)

- `PUT http://localhost:8000/v1/admin/projects/qa-project/connections/qa-conn-a`
- Auth: `admin_token` or `editor_token`
- Body:
  ```json
  {
    "source_type": "local_file",
    "name": "QA Conn A",
    "credentials": {},
    "config": { "source_path": "/data/sources" }
  }
  ```
- Expected: `201`

**4) Connection `qa-conn-b`** in `qa-project` (sibling, used by AC2)

- Same as step 3, URL `.../connections/qa-conn-b`, `"name": "QA Conn B"`
- Expected: `201`

All requests below use the format: **Method**, **URL**, **Auth**,
**Headers** (besides Authorization), **Body**, **Expected**.

---

## AC1 — Reading a single level

### Fixture: cascade 512 / 384 / 256

**Tenant = 512**

- `PATCH http://localhost:8000/v1/admin/settings?level=tenant`
- Auth: `admin_token` · Headers: `If-Match: W/"rev-0"`
- Body: `{ "chunking.max_tokens": 512 }`

**Project qa-project = 384**

- `PATCH http://localhost:8000/v1/admin/settings?level=project:qa-project`
- Auth: `admin_token` · Headers: `If-Match: W/"rev-0"`
- Body: `{ "chunking.max_tokens": 384 }`

**Connection qa-project/qa-conn-a = 256**

- `PATCH http://localhost:8000/v1/admin/settings?level=connection:qa-project/qa-conn-a`
- Auth: `admin_token` · Headers: `If-Match: W/"rev-0"`
- Body: `{ "chunking.max_tokens": 256 }`

### Test cases

| # | Request | Expected |
|---|---------|----------|
| 1 | `GET ?level=project:qa-project` as `admin_token` | `200`, `ETag: W/"rev-N"`, `values.chunking.max_tokens = 384` |
| 2 | Same, as `editor_token` | `403` |
| 3 | `GET ?level=connection:qa-project/qa-conn-a` as `editor_token` | `200` |
| 4 | `GET ?level=project:nope` as `admin_token` | `422`, `error.code = "reference_not_found"` |
| 5 | `GET ?level=planet:x` as `admin_token` | `422`, `error.details=[{"field":"level",...}]` |
| 6 | `GET ?level=tenant` as `admin_token` | `200`, body contains no occurrence of `secret` |

---

## AC2 — Effective value and precedence

| # | Request | Expected |
|---|---------|----------|
| 1 | `GET ?level=connection:qa-project/qa-conn-a&effective=true` (editor) | `chunking.max_tokens = {value: 256, origin: "connection"}` |
| 2 | `GET ?level=connection:qa-project/qa-conn-b&effective=true` (editor) | `chunking.max_tokens = {value: 384, origin: "project"}` |
| 3 | `GET ?level=project:qa-project-2&effective=true` (admin) | `chunking.max_tokens = {value: 512, origin: "tenant"}` |
| 4 | `GET ?level=tenant&effective=true` (admin) | `ingestion.max_attempts = {value: 3, origin: "default"}` |

---

## AC3 — Changing one setting

1. `GET ?level=project:qa-project` → note the `ETag` (e.g. `W/"rev-1"`).
2. `PATCH ?level=project:qa-project`, `If-Match: W/"rev-1"`,
   `{ "chunking.max_tokens": 300 }` →
   `200`, `ETag` incremented (`W/"rev-2"`),
   `changed: [{key, from: 384, to: 300, applies: "next_ingestion"}]`.
3. `GET` again → `chunking.max_tokens = 300`, ETag matches new revision.
4. Same PATCH as `editor_token` → `403`.

> `source`/`actor` of a revision are not exposed over HTTP yet (reserved for
> `GET /settings/revisions` in v2.1); only in the `setting_scope_revisions`
> table (`select tenant_id, scope_level, scope_id, revision, source, actor
> from setting_scope_revisions;`), or via
> `test_ac3_admin_patches_project_level_and_bumps_the_revision`.

---

## AC4 — Dry run and deleting an override

1. `PATCH ?level=project:qa-project&dry_run=true`, current `If-Match`,
   `{ "chunking.max_tokens": 300 }` → `200`, `dry_run: true`.
2. `GET` → value and ETag unchanged.
3. `PATCH ?level=project:qa-project`, current `If-Match`,
   `{ "chunking.max_tokens": null }` → `200` (override deleted).
4. `GET ?level=project:qa-project&effective=true` →
   `chunking.max_tokens = {value: 512, origin: "tenant"}` (falls back to tenant).

---

## AC5 — Rejected changes

All `PATCH http://localhost:8000/v1/admin/settings?level=project:qa-project`
as `admin_token` (except (g), `level=tenant`). Fetch a fresh `If-Match`
before each case, except (e) and (f) as noted.

| Case | Headers | Body | Expected |
|---|---|---|---|
| (a) misspelled key | current `If-Match` | `{ "chunking.max_tokenz": 1 }` | `422` |
| (b) value out of range | current `If-Match` | `{ "chunking.max_tokens": 99999 }` | `422` |
| (c) key not allowed at this level | current `If-Match` | `{ "ingestion.concurrency": 4 }` | `422` |
| (d) unknown key | current `If-Match` | `{ "database.url": "postgres://..." }` | `422` |
| (e) missing `If-Match` | _(omitted)_ | `{ "chunking.max_tokens": 300 }` | `422` |
| (f) stale `If-Match` | `W/"rev-0"` | `{ "chunking.max_tokens": 300 }` | `412` |
| (g) embedding model change (tenant level) | current tenant `If-Match` | `{ "models.embedding": "prov_openai:text-embedding-3-small" }` | `409 reindex_required` |

**Final check**: `GET ?level=project:qa-project` — `values`/`revision` must
be identical to before all 7 cases (nothing was persisted).

---

## AC6 — First-time seed and drift detection

Start-up-only behavior (seed once from `config/runtime_settings.yaml` when
the DB is empty), not an HTTP request — **cannot be tested via Postman**
without truncating `setting_overrides`, `setting_scope_revisions`, and the
`settings_yaml_seed` marker, which destroys any real data already in that
DB. Use pytest:

```bash
uv run pytest packages/harborrag-runtime/tests/unit/test_settings_seed.py -v
```

| Test | Verifies |
|---|---|
| `test_seeds_empty_database_once` | first seed, revision `source="seed"` |
| `test_second_boot_with_unchanged_file_does_not_reseed` | restart doesn't reseed, revision unchanged |
| `test_changed_file_logs_drift_without_reimporting` / `test_drift_log_names_the_key_that_changed` | editing the file post-seed logs `WARN config_yaml_drift ... keys=...`, DB value unchanged |

If you must test manually, **use a dedicated Postgres**, not the one used
for AC1–5:

```sql
TRUNCATE setting_overrides, setting_scope_revisions;
UPDATE workspace_settings SET data = '{}'::json;
```

```bash
echo 'tenant: DEFAULT
settings:
  chunking.max_tokens: 512' > config/runtime_settings.yaml
# restart the service
# -> effective value = 512, origin=tenant; revision source="seed"

echo 'tenant: DEFAULT
settings:
  chunking.max_tokens: 1024' > config/runtime_settings.yaml
# restart again
# -> effective value STAYS 512; one WARN config_yaml_drift line; no new revision
```

---

## AC8 — Parsing and chunking applied to real ingestion

**Expected behavior**: a connection with `parsing.pdf_parser` and/or
`chunking.max_tokens` overridden anywhere in the tenant→project→connection
chain must have every document of its next ingestion use the overridden
engine/token cap — not the registry default — and must not reuse an old
artifact when only the setting changed. The run must carry the real
`settings_revision` of that connection scope.

Reuse the `qa-project`/`qa-conn-a` fixture from section 0.

| # | Request | Expected |
|---|---------|----------|
| 1 | `PATCH ?level=connection:qa-project/qa-conn-a`, current `If-Match`, body `{ "parsing.pdf_parser": "liteparse", "chunking.max_tokens": 64 }` (admin) | `200`, `changed` lists both keys |
| 2 | `POST /v1/admin/ingestions`, body `{ "project_id": "qa-project", "connection_id": "qa-conn-a", "mode": "force" }` (editor) | `202`, `Location: /v1/admin/ingestions/{id}` |
| 3 | `GET /v1/admin/ingestions/{id}` (reader) | `settings_revision` matches the connection scope's current revision (not `configuration_fingerprint`) |
| 4 | `GET /v1/admin/ingestions/{id}` polled until done | `status: SUCCESS` |
| 5 | Read `document_versions.chunk_artifact` from Postgres + the referenced `.jsonl` from MinIO (bucket `harborrag-artifacts`, key prefixed `harborrag_<tenant>/`) | every `record_kind: evidence` chunk's `token_count` ≤ overridden `chunking.max_tokens`; `record_kind: route` chunks are capped separately by `ROUTE_MAXIMUM_TOKENS`, unaffected by `chunking.max_tokens` |

`parsing.pdf_parser=docling` cannot be exercised with real PDF content
unless `config/parsers.yaml` enables the `docling` block and the `docling`
package is installed in the worker image — otherwise a request forcing it
is rejected with `UnknownPDFEngineError` (same as a typo'd engine name),
which is expected, not a failure of the override mechanism.

---

## Common troubleshooting

| Symptom | Cause / Fix |
|---|---|
| `404 { "code": "not_found" }` on any `/v1/admin/settings*` route | API container is running an old build. `./scripts/deployment/dev.sh api --build`. |
| `401`, log says `invalid token lifetime` | Token's `exp - iat` exceeds `HARBORRAG_AUTH_MAX_TOKEN_LIFETIME_SECONDS` (default 3600s). Mint a shorter-lived token, or raise the env var (max 86400s) and restart the API. |
| `422 reference_not_found` | `project_id`/`connection_id` in the URL doesn't exist for the token's tenant. Rebuild the fixture, or check with `GET /v1/admin/projects`. |

---

## AC9 — Temporal settings reach the workflow

**Expected behavior**: a connection with
`{"ingestion.concurrency": 2, "ingestion.activity_timeout": "PT2M",
"ingestion.max_attempts": 2}` overridden, then force-ingested, produces a
Temporal run whose document activity has a 2-minute start-to-close timeout,
a 2-attempt retry policy, and at most 2 documents processed concurrently. A
document that permanently fails after candidate creation appears via
`GET /v1/admin/failures?connection=...` with `attempts = 2`.

```bash
uv run pytest packages/harborrag-app/tests/test_connection_service.py -k "ac9 or ac10" -v
uv run pytest packages/harborrag-runtime/tests/runtime_ingestion/unit/temporal/test_client.py -k "overlay or deployment_defaults" -v
```

- `test_ac9_ingestion_concurrency_timeout_and_max_attempts_reach_the_prepared_source` —
  all 3 keys override correctly (`"PT2M"` → 120s).
- `test_ac9_unset_ingestion_settings_stay_none` — no override → all 3 stay
  `None` (worker/deployment default, not the registry default).
- `test_client_overlays_a_connections_activity_timeout_and_max_attempts` —
  `IngestionTemporalClient.start()` overlays the 2 overridden values onto
  Temporal's `workflow_options` while leaving unrelated fields
  (`task_queues`, `timeouts.discovery_seconds`) at the deployment default.
- `test_client_leaves_workflow_options_at_deployment_defaults_without_overrides` —
  no overrides → `workflow_options` unchanged from deployment config.
- `test_ac10_a_running_ingestions_resolved_settings_survive_a_later_patch` —
  AC10: a run's settings are resolved once at submit time and are immune to
  a PATCH issued after that run already started; the next run sees the new
  revision.

### AC9 part 2 — `GET /v1/admin/failures?connection=...`

```bash
uv run pytest packages/harborrag-adapters/tests/repositories/unit/test_ingestion_control_reliability.py -k list_failures -v
uv run pytest packages/harborrag-app/tests/test_failures_service.py -v
uv run pytest packages/harborrag-app/tests/test_export_openapi.py -v
```

Postman (`reader_token` or above):

| # | Request | Expected |
|---|---------|----------|
| 1 | `GET /v1/admin/failures?connection=qa-project/qa-conn-a` | `200`, `items: []` if the connection has no failed documents recorded (see note below on what actually gets recorded) |

**Not every failed ingestion produces an item here.** A document that fails
to parse (e.g. a corrupted file) is rejected before its
`document_version_id`/candidate is created, so it's classified as
`skipped_by_reason: EXTRACTION_FAILED` at the task level and never written
to the `document_failures` table this route reads from. Only failures that
happen **after** the candidate exists (`EncodeChunks`, `ChunkAndValidate`,
`PersistCanonical` — storage/embedding errors, not malformed content) are
recorded here.

To exercise this route without reproducing a real infra failure, seed a row
directly, attached to a real `document_id`/`document_version_id` that was
already ingested successfully:

```sql
INSERT INTO document_failures (
  failure_id, document_id, document_version_id, failed_stage,
  failure_category, retryable, safe_error_code, artifact_references,
  projection_manifest_reference, first_failure_at, last_failure_at,
  attempt_count
) VALUES (
  'failure:demo-0001',
  '<real document_id>', '<real document_version_id>',
  'EncodeChunks', 'ENCODER_FAILURE', false, 'embedding_model_unreachable',
  '[]'::jsonb, NULL, now(), now(), 2
);
```

`failure_category` must be one of the `FailureCategory` enum values
(`harborrag_core/ingestion/states.py`): `TRANSIENT`, `RATE_LIMITED`,
`PARSER_FALLBACKABLE`, `UNSUPPORTED`, `SOURCE_FORBIDDEN`,
`INVALID_CONFIGURATION`, `CANONICAL_VALIDATION`, `CHUNK_VALIDATION`,
`ENCODER_FAILURE`, `VECTOR_WRITE_FAILURE` — any other string makes the route
return `500`. Delete the seeded row after testing.

---

## AC11 — Database unavailable

**Expected behavior**: stopping Postgres after boot must not crash the
process; `GET /readyz` reports `postgres: fail` while down; a setting
PATCHed just before the stop must survive and apply correctly once Postgres
recovers, within ~10 seconds.

| # | Request | Expected |
|---|---------|----------|
| 1 | `PATCH ?level=connection:qa-project/qa-conn-a`, body `{ "chunking.max_tokens": ... }`, while Postgres is healthy | `200`, revision bumps |
| 2 | `docker stop compose-postgres-1`, then `GET /readyz` | `503`, `{"postgres":"fail","redis":"ok","temporal":"ok","qdrant":"ok","graph":"ok"}` |
| 3 | `GET /healthz` (Postgres still down) | `200`, `{"status":"ok"}` — process does not crash |
| 4 | `docker start compose-postgres-1`, then poll `GET /readyz` | returns to `ready` within seconds, no container restart needed |
| 5 | `GET /v1/admin/settings?level=connection:qa-project/qa-conn-a` | the PATCH from step 1 is intact |
| 6 | `POST /v1/admin/ingestions` (force) | `202`, `settings_revision` matches the value from step 1 |
| 7 | `GET /v1/admin/ingestions/{id}` polled until done | `status: SUCCESS`; chunk token counts respect the overridden cap (same method as AC8 step 5) |
