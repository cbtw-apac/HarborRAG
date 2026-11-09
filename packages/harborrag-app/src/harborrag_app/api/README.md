# Control Plane API

Run HarborRAG's HTTP API, find its routes, and look up its errors, authentication, limits
and settings. These are contributor notes for the `harborrag_app.api` package.

The package is HarborRAG's FastAPI boundary. It validates HTTP input, authenticates
callers, delegates every operation to the transport-neutral application service, and maps
results onto stable HTTP responses. Connector execution, parsing, chunking, indexing and
Temporal workflow rules stay outside this package.

The public API lives under `/v1`. The probes `/healthz` and `/readyz` sit at the root, and
Prometheus `/metrics` answers only on the separate ops port. `/` redirects to `/docs`
while the documentation is enabled, and to `/healthz` when it is disabled.

## Install and run

Install the API dependencies from a repository checkout:

```bash
uv sync --package harborrag-app --extra api
```

Start the API on its public port and its ops port:

```bash
HARBORRAG_PORT=8000 HARBORRAG_OPS_PORT=9465 \
  uv run python -m harborrag_app.api.serve
```

`python -m harborrag_app.api.serve` runs one application on two sockets. Plain
`uvicorn harborrag_app.api.app:create_fastapi_app --factory` still works, but then no
socket serves `/metrics`.

For the local Docker stack, start everything and check the probe:

```bash
scripts/deployment/dev.sh up
curl --fail http://127.0.0.1:8000/healthz
```

Which `env/` file holds the API's values, the loopback-only port publishing, the
generated metrics scrape token and `dev.sh api` for the API alone are in the
[deployment guide](../../../../../docs/developers/deployment/README.md#run-the-api-and-worker).

## Composition

The application lifespan builds one `AppService` through the runtime's production
composition and closes it at process shutdown. Its control database is
`HARBORRAG_CONTROL_DB_URL`, a local SQLite file by default. Outside
`HARBORRAG_ENV=dev`, SQLite and `HARBORRAG_AUTH_MODE=none` are both refused at startup.

## Routes

The live schema at `/openapi.json` is authoritative; `x-min-role` on each operation gives
its lowest role.

| Method | Path | Lowest role | Behavior |
| --- | --- | --- | --- |
| `GET` | `/healthz` | none | Liveness; calls no dependency |
| `GET` | `/readyz` | none | `200` when Postgres, Redis, Temporal, Qdrant and the graph store answer, else `503`; same body both ways |
| `GET` | `/v1/me` | `reader` | The credential's principal, tenants and role |
| `GET` | `/v1/admin/source-types` | `editor` | Source types with their credential and config schemas |
| `GET` | `/v1/admin/connections` | `editor` | Connections in the tenant, cursor-paged |
| `GET` `PUT` `DELETE` | `/v1/admin/projects/{project_id}/connections/{connection_id}` | `editor` | Read, create or update, delete a connection |
| `POST` | `/v1/admin/projects/{project_id}/connections/{connection_id}/test` | `editor` | Test a connection |
| `POST` `GET` | `/v1/admin/ingestions` | `editor` | Start an ingestion of the connection named in the body; list runs (`project_id`, `connection_ref`, `status`, `created_after`, `limit`, `cursor`) |
| `GET` | `/v1/admin/ingestions/{ingestion_id}` | `editor` | One run in the explorer `Ingestion` shape |
| `GET` | `/v1/admin/ingestions/{ingestion_id}/documents` | `editor` | Per-document results, `?status=all\|succeeded\|skipped\|failed` |
| `GET` | `/v1/admin/ingestions/{ingestion_id}/events` | `editor` | Server-Sent Events of one run: `run.status`, `stage.progress`, `document.failed`, `run.completed` |
| `POST` | `/v1/admin/ingestions/{ingestion_id}/pause`, `/resume`, `/cancel` | `editor` | `200` with the run after waiting for the new status; `409 invalid_state` from a state that cannot take the action |
| `POST` | `/v1/admin/ingestions/{ingestion_id}/retry-failures` | `editor` | `202` with the new run and `Location` |
| `GET` | `/v1/admin/failures` | `editor` | Failure inbox with `counts` and optional `total` |
| `POST` | `/v1/admin/failures/retry` | `editor` | Retry failures by `ids` or `filter`; one run per original run; `409` when none match |
| `GET` | `/v1/admin/settings` | `editor` for `connection:` levels, else `admin` | Read the overrides or the effective values at a `level` |
| `PATCH` | `/v1/admin/settings` | `admin` | Change settings at a `level`, with `If-Match` |
| `GET` | `/v1/admin/settings/schema` | `editor` | The six runtime settings by group: type, range, levels, when a change applies, description |
| `GET` | `/v1/admin/projects` | `admin` | Projects in the tenant, cursor-paged |
| `GET` `PUT` `DELETE` | `/v1/admin/projects/{project_id}` | `admin` | Read, create or update, delete a project |
| `GET` | `/v1/projects/{project_id}/connections` | `reader` | Connections a reader can search in a project |
| `POST` `GET` | `/v1/chat/sessions` | `reader` | Create a session scoped to one project for its life (`422 reference_not_found` for a project the tenant lacks); list the caller's own, newest first, `q` searches titles |
| `GET` | `/v1/chat/sessions/{session_id}/messages` | `reader` | A session's messages, newest first, with citations and `trace_id`; `404` for another reader's session |
| `POST` | `/v1/chat/completions` | `reader` | Answer one question in mode `instant` or `classic`, in a session (stored in its history under `completion_id`) or as a one-off; the explorer completion body with `[n]` citations and `usage`, as JSON or SSE |

The request and response details of the ingestion and failure routes, including the
event stream's frames, resume and deadline, are in
[Start and control ingestions](../../../../../docs/users/connections.md#start-and-control-ingestions)
and [Review and retry failures](../../../../../docs/users/connections.md#review-and-retry-failures).
The settings calls are in
[Runtime settings](../../../../../docs/users/configuration/runtime-settings.md#read-and-change-settings-over-http),
and the chat routes in the [Chat guide](../../../../../docs/users/chat/README.md).

A run in another tenant is `404 not_found`, and these routes take no `?tenant_id`
parameter. A failure's `stage` is one of `capture`, `interpret`, `commit`, `derive` and
`publish`, mapped from the workflow activity that failed:

| Activity | Stage |
| --- | --- |
| `FetchAndCaptureRaw` | `capture` |
| `ParseAndNormalize`, `ChunkAndValidate` | `interpret` |
| `PersistCanonical`, `SyncContentUnits` | `commit` |
| `EncodeChunks`, `BuildProjections`, `WriteVectorProjection`, `WriteGraphProjection`, `BuildRelations` | `derive` |
| `VerifyProjections`, `PublishVersion` | `publish` |
| Any other activity | `capture` |

The `links` of a run point at the Temporal and Grafana UIs named by
`HARBORRAG_TEMPORAL_UI_URL` and `HARBORRAG_GRAFANA_URL`, and are empty strings when those
are unset. `links.langfuse` and `trace_id` are empty, and `run_id` is the run's
`ingestion_id`.

Every route that predates v2.0.0, including `/api/v1/*`, `/v1/connections`,
`/v1/ingestions*`, `/v1/agent/*`, `/v1/retrieval/*`, `/v1/graph/*`, `/v1/memory/*` and
`/v1/schedules/*`, answers `404 not_found` with no deprecated alias. Retrieval has no
HTTP route: it is served by the CLI, the MCP tools and chat. The replacements are listed
in [Removed routes](../../../../../docs/users/connections.md#removed-routes).

## Errors and request tracing

Every 4xx and 5xx response, except `/readyz`, has `Content-Type: application/json` and
one envelope:

```json
{
  "error": {
    "code": "validation_error",
    "message": "Request validation failed",
    "details": [{"field": "name", "issue": "Input should be a valid string"}],
    "trace_id": "4bf92f3577b34da6a3ce929d0e0e4736"
  }
}
```

- `code` comes from the catalog in `errors.CATALOG`; clients branch on `code`, never on
  `message`.
- `details` is `null`, an object, or for `validation_error` a list of `{field, issue}`.
  A body that is not valid JSON fails on field `body`.
- A connection failure no service translated is `503 dependency_unavailable`, with
  `details.dependency` naming `postgres`, `redis`, `temporal`, `qdrant` or `graph`.
- A `405` keeps its `Allow` header and uses code `not_found`.

Every response carries `X-Trace-Id`, 32 lowercase hex characters that also appear in the
logs. The API keeps a valid `X-Trace-Id` or W3C `traceparent` the caller sends, and mints
one otherwise.

## Authentication, roles and tenants

`HARBORRAG_AUTH_MODE` takes one of:

- `none`: local development only; every request is an implicit `owner` principal.
- `hmac`: verifies HS256 bearer JWTs with `HARBORRAG_AUTH_SECRET`.
- `oidc`: reserved and not implemented; application creation fails fast.

`HARBORRAG_ENV=staging` and `prod` refuse `none`. HMAC secrets must hold at least 32 UTF-8
bytes. Tokens must carry the `sub`, `role`, `tenants`, `iat`, `exp`, `iss` and `aud`
claims, and may carry `name`. `HARBORRAG_AUTH_USER_ID_CLAIM` selects the signed end-user
claim (`sub` by default).

An MCP reader key (`hrk_<env>_v1_...`) is accepted as the same bearer credential. It
authenticates as role `reader` in its one tenant, and `GET /v1/me` reports it as
`principal.principal_type = "api_key"`.

Each route checks, in order, the credential, the tenant and the role; a failed check
never reaches the handler:

- A missing, invalid or expired credential is `401 unauthenticated` with
  `WWW-Authenticate: Bearer`, and one message whatever check failed.
- The tenant comes from the credential. A credential with one tenant needs no header; a
  wildcard (`*`) credential without `X-Tenant-Id` acts in `DEFAULT`. A credential with
  several tenants names one with `X-Tenant-Id`, else every route except `/v1/me` answers
  `400 tenant_required`. A tenant the credential does not hold is `403 tenant_forbidden`.
  See [Tenants](../../../../../docs/users/connections.md#tenants).
- A role below the route's lowest role is `403 forbidden`. Roles stack:
  `reader < editor < admin < owner`.
- Another tenant's resource reads as `404 not_found`, never `403`.

## Rate limits and deadlines

Each `/v1` call is charged, after the checks above pass, to a sliding one-second window
per principal: `HARBORRAG_HTTP_RATE_DEFAULT` (10) calls per second, or
`HARBORRAG_HTTP_RATE_CHAT` (2) for `POST /v1/chat/completions`. A full window is
`429 rate_limited` with an integer `Retry-After`. The windows live in the capacity Redis
when it is configured; when Redis is unreachable, calls go through. The probes and
`/metrics` are never limited.

Chat completions and the ingestion and failure routes also pass a per-minute and
in-flight capacity limiter at the user, principal and tenant scopes. Its variables and
rules are in
[Request rates and concurrency](../../../../../docs/users/chat/limits.md#request-rates-and-concurrency).

A handler that runs past `HARBORRAG_API_REQUEST_TIMEOUT_SECONDS` (120) is
`504 deadline_exceeded`; a chat completion with `stream: true` is answered within it too,
before its stream opens. A streamed ingestion event body has its own
`HARBORRAG_API_STREAM_TIMEOUT_SECONDS` (600) and ends with an `error` frame.

## Writes and paging

| Mechanism | Rule |
| --- | --- |
| `ETag` | `W/"rev-<n>"` on reads and successful writes of projects and connections |
| `If-Match: <etag>` | The write fails with `412 precondition_failed` unless it matches |
| `If-Match: *` | Connection `PUT` updates only |
| `If-None-Match: *` | Project and connection `PUT` create only |
| `Idempotency-Key` | `POST /v1/chat/completions` only: the same key and body within 24 h replay the stored answer without a model call; see [Retry without duplicates](../../../../../docs/users/chat/limits.md#retry-without-duplicates) |
| Paging | `?limit=` 1-100 (20 by default) and `?cursor=`; the body is `{"items": [...], "next_cursor": null}`. A cursor the server did not issue is `422` on field `cursor` |

## Metrics

`GET /metrics` answers only on `HARBORRAG_OPS_PORT` (9465), and only to
`Authorization: Bearer <HARBORRAG_METRICS_SCRAPE_TOKEN>`. Any other credential, a user
token included, is `401`; any other path on the ops port is `404`, and the public port has
no `/metrics`. The body is the API registry in the Prometheus
`text/plain; version=0.0.4` format: HTTP request counters and durations by route
template, build information and process metrics. Ingestion counters come from the
worker's own metrics port.

The monitoring stack's Prometheus must send the same token; how it reads it, and what a
mismatch looks like, is in
[Run the monitoring stack](../../../../../docs/developers/deployment/README.md#run-the-monitoring-stack).

## Settings

API process settings use the `HARBORRAG_` prefix. The model is `ApiSettings` in
`settings.py`; the runtime variables at the end of the table come from `RuntimeSettings`.

| Variable | Default | Purpose |
| --- | --- | --- |
| `HARBORRAG_HOST` | `127.0.0.1` | Bind host for both sockets; containers override it to `0.0.0.0` |
| `HARBORRAG_PORT` | `8000` | Public port |
| `HARBORRAG_OPS_PORT` | `9465` | Ops port serving `/metrics`; must differ from the public port |
| `HARBORRAG_METRICS_SCRAPE_TOKEN` | unset | Bearer token Prometheus sends to `/metrics`; unset refuses every scrape |
| `HARBORRAG_ENV` | `dev` | `dev`, `staging` or `prod` process policy |
| `HARBORRAG_AUTH_MODE` | `none` | `none`, `hmac` or `oidc` |
| `HARBORRAG_ALLOW_INSECURE_DEV` | `false` | Acknowledge disabled auth on a non-loopback development listener |
| `HARBORRAG_AUTH_SECRET` | unset | Shared HS256 secret |
| `HARBORRAG_AUTH_ISSUER` | `harborrag` | Required JWT issuer |
| `HARBORRAG_AUTH_AUDIENCE` | `harborrag-api` | Required JWT audience |
| `HARBORRAG_AUTH_USER_ID_CLAIM` | `sub` | Signed end-user claim used for session and memory ownership |
| `HARBORRAG_CORS_ORIGINS` | `[]` | JSON list of allowed browser origins |
| `HARBORRAG_DOCS_ENABLED` | `true` in `dev` | Enables Swagger at `/docs` and `/openapi.json` |
| `HARBORRAG_MAX_REQUEST_BODY_BYTES` | `1048576` | Largest body accepted before parsing |
| `HARBORRAG_HTTP_RATE_DEFAULT` | `10` | Calls per second per principal on every `/v1` route |
| `HARBORRAG_HTTP_RATE_CHAT` | `2` | Chat completions per second per principal |
| `HARBORRAG_API_CAPACITY_REDIS_URL` | unset | Redis for the rate windows and the capacity leases; required outside `dev` |
| `HARBORRAG_API_CAPACITY_ALLOW_INSECURE_REMOTE` | `false` | Development-only acknowledgement for remote plaintext Redis |
| `HARBORRAG_API_REQUESTS_PER_MINUTE` | `60` | Capacity-limited calls per principal per minute |
| `HARBORRAG_API_MAX_INFLIGHT_PER_PRINCIPAL` | `4` | Concurrent capacity-limited calls per principal |
| `HARBORRAG_API_REQUEST_TIMEOUT_SECONDS` | `120` | Deadline of every `/v1` handler |
| `HARBORRAG_API_STREAM_TIMEOUT_SECONDS` | `600` | Deadline of one streamed body |
| `HARBORRAG_CONTROL_DB_URL` | local SQLite | Control-plane SQLAlchemy URL |
| `HARBORRAG_SETTINGS_SEED_PATH` | `config/settings.yaml` | Seed file for runtime settings, read once when the tenant has no revisions; later differences log `config_yaml_drift` |
| `HARBORRAG_TEMPORAL_CONFIG_PATH` | `config/temporal.yaml` | Temporal connection, queues, retries, worker and workflow configuration |
| `HARBORRAG_TEMPORAL_TARGET` | value from YAML | Optional Temporal frontend override |
| `HARBORRAG_TEMPORAL_API_KEY` | unset | Secret API credential; keep it outside tracked YAML |
| `HARBORRAG_TEMPORAL_UI_URL` | unset | Base URL of the Temporal UI, used for `links.temporal` of a run |
| `HARBORRAG_GRAFANA_URL` | unset | Base URL of Grafana, used for `links.grafana` of a run |
| `HARBORRAG_LOG_LEVEL` | `INFO` | HarborRAG namespace log level |

The per-user and per-tenant capacity variables are in
[Limits and accounting](../../../../../docs/users/chat/limits.md#request-rates-and-concurrency).
Credentialed CORS rejects wildcard origins. Swagger and `/openapi.json` default to
disabled under `HARBORRAG_ENV=staging` and `prod` unless an operator enables them.

## OpenAPI

In development, the API serves Swagger at `http://127.0.0.1:8000/docs` and the schema at
`http://127.0.0.1:8000/openapi.json`. Export the deterministic schema without starting
the server:

```bash
uv run python -m harborrag_app.api.export_openapi > openapi.json
```

## Package boundary

```text
api/
├── app.py, router.py, settings.py, serve.py, dependencies.py
├── errors.py, dependency_failures.py, middleware.py, schemas.py, sse.py
├── auth/                 # credentials, tenants, the ordered role guard
├── probes.py, readiness.py
├── ops.py, metrics.py    # the ops port and its Prometheus registry
├── rate_limit.py, deadline.py, capacity*.py
├── pagination.py, openapi.py, export_openapi.py
└── v1/                   # the public /v1 routes
    ├── admin/            # source types, connections, projects and settings
    ├── chat/             # chat sessions and completions
    ├── ingestion/        # ingestion runs and the failure inbox
    └── me.py
```

This package may validate and serialize HTTP messages, authenticate principals and
enforce route roles, manage the API process and its middleware, and delegate operations
through `BaseAppService`. It must not instantiate connectors, parsers, model clients or
repositories, implement ingestion state transitions, expose Temporal SDK handles on the
wire, or return provider exception details or secrets.

Run its tests from the repository root:

```bash
uv run pytest packages/harborrag-app/tests/test_api_*.py packages/harborrag-app/tests/test_export_openapi.py
```
