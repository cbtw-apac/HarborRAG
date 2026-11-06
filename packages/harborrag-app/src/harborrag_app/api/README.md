# Control Plane API

The `harborrag_app.api` package is HarborRAG's FastAPI boundary. It validates
HTTP input, authenticates callers, delegates workflow operations to the
transport-neutral application service, and maps results onto stable HTTP
responses. Connector execution, parsing, chunking, indexing, and Temporal
workflow rules remain outside this package.

The public API lives under `/v1`. The probes `/healthz` and `/readyz` sit at
the root, and Prometheus `/metrics` answers only on the separate ops port.

`/` redirects to `/docs` while documentation is enabled, and to `/healthz`
when it is disabled.

## Install and run

Install the API dependencies from a source checkout:

```bash
uv sync --package harborrag-app --extra api
```

Start the API on its public port and its ops port:

```bash
HARBORRAG_PORT=8000 HARBORRAG_OPS_PORT=9465 \
  uv run python -m harborrag_app.api.serve
```

`python -m harborrag_app.api.serve` runs one application on two sockets.
Plain `uvicorn harborrag_app.api.app:create_fastapi_app --factory` still works,
but then no socket serves `/metrics`.

For the supported local Docker topology, use:

```bash
scripts/deployment/dev.sh up
curl --fail http://127.0.0.1:8000/healthz
curl --fail -H "Authorization: Bearer $(cat env/api-scrape-token)" \
  http://127.0.0.1:9465/metrics
```

The development helper starts the data services, Temporal, an ingestion
worker, and the API, then waits for its health check. If those dependencies are
already running, `scripts/deployment/dev.sh api` starts only the API. API process
and authentication values belong in the ignored `env/.env.api`; embedding
credentials come from `env/.env.models`. Connector credentials remain isolated
in the worker. See the repository
[deployment guide](../../../../../docs/developers/deployment/README.md) for
environment preparation and shutdown.

The local API container listens on `0.0.0.0` inside Docker, but Compose
publishes port 8000 and the ops port 9465 only on host loopback. The
development environment file therefore sets `HARBORRAG_ALLOW_INSECURE_DEV=true`
alongside `HARBORRAG_AUTH_MODE=none`. Use HMAC auth before exposing the API
outside the trusted local development network.

## Composition modes

The application lifespan constructs one `AppService` and closes it on process
shutdown.

| Condition | Composition |
| --- | --- |
| `HARBORRAG_ENV=dev` without `HARBORRAG_CONTROL_DB_URL` | Development control-database stub plus the real Temporal client |
| A control-database URL is set | Migrated runtime control-plane repositories plus the real Temporal client |
| `HARBORRAG_ENV=prod` | Requires an explicit control-database URL and authenticated API mode |

## HTTP routes

The live schema at `/openapi.json` is authoritative; `x-min-role` on each
operation gives its lowest role.

| Method | Path | Lowest role | Behavior |
| --- | --- | --- | --- |
| `GET` | `/healthz` | none | Liveness; calls no dependency |
| `GET` | `/readyz` | none | `200` when Postgres, Redis, Temporal, Qdrant and the graph store answer, else `503`; same body both ways |
| `GET` | `/v1/me` | `reader` | The credential's principal, tenants and role |
| `GET` | `/v1/admin/source-types` | `editor` | Source types with their credential and config schemas |
| `GET` | `/v1/admin/connections` | `editor` | Connections in the tenant, cursor-paged |
| `GET` `PUT` `DELETE` | `/v1/admin/projects/{project_id}/connections/{connection_id}` | `editor` | Read, create or update, delete a connection |
| `POST` | `/v1/admin/projects/{project_id}/connections/{connection_id}/test` | `editor` | Test a connection |
| `POST` `GET` | `/v1/admin/ingestions` | `editor`, `reader` | Start an ingestion of the connection named in the body; list runs |
| `GET` `POST` | `/v1/admin/ingestions/{ingestion_id}[/documents\|/events\|/pause\|/resume\|/cancel\|/retry-failures]` | `reader`, `editor` | Read and control one run |
| `GET` `PATCH` | `/v1/admin/settings` | `admin`; `editor` reads `connection:` levels | Read overrides or effective values at a `level`; change them with `If-Match` |
| `GET` | `/v1/admin/settings/schema` | `editor` | The six runtime settings by key: type, range, levels, when a change applies |
| `GET` | `/v1/admin/projects` | `admin` | Projects in the tenant, cursor-paged |
| `GET` `PUT` `DELETE` | `/v1/admin/projects/{project_id}` | `admin` | Read, create or update, delete a project |
| `GET` | `/v1/projects/{project_id}/connections` | `reader` | Connections a reader can search in a project |
| `POST` `GET` | `/v1/chat/sessions` | `reader` | Create a chat session, list the caller's sessions |
| `GET` | `/v1/chat/sessions/{session_id}/messages` | `reader` | A session's messages |
| `POST` | `/v1/chat/completions` | `reader` | Retrieval-grounded JSON or SSE chat completion |

The ingestion read and control routes keep the task shape they had under
`/v1/ingestions`. Every other route that predates v2.0.0, including `/api/v1/*`,
`/v1/connections`, `/v1/ingestions*`, `/v1/agent/*`, `/v1/retrieval/*`,
`/v1/graph/*`, `/v1/memory/*` and `/v1/schedules/*`, answers `404 not_found`
with no deprecated alias.

## Errors and request tracing

Every 4xx and 5xx response, except `/readyz`, has `Content-Type:
application/json` and one envelope:

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

- `code` comes from the catalog in `errors.CATALOG`; clients branch on `code`,
  never on `message`.
- `details` is `null`, an object, or for `validation_error` a list of
  `{field, issue}`. A body that is not valid JSON fails on field `body`.
- A connection failure no service translated is `503 dependency_unavailable`
  with `details.dependency` naming `postgres`, `redis`, `temporal`, `qdrant`
  or `graph`.
- A `405` keeps its `Allow` header and uses code `not_found`.

Every response carries `X-Trace-Id`, 32 lowercase hex characters that also
appear in the logs. The API keeps a valid `X-Trace-Id` or W3C `traceparent`
the caller sends, and mints one otherwise.

## Authentication, roles and tenants

`HARBORRAG_AUTH_MODE` supports:

- `none`: local development only; assigns an implicit `owner` principal;
- `hmac`: verifies HS256 bearer JWTs using `HARBORRAG_AUTH_SECRET`;
- `oidc`: reserved but not implemented; application creation fails fast.

Production refuses `auth_mode=none`. HMAC secrets must contain at least 32
UTF-8 bytes. Tokens must contain `sub`, `role`, `tenants`, `iat`, `exp`, `iss`
and `aud` claims, and may carry `name`. `HARBORRAG_AUTH_USER_ID_CLAIM` selects
the signed end-user claim (`sub` by default).

An MCP reader key (`hrk_<env>_v1_...`) is accepted as the same bearer
credential. It authenticates as role `reader` in its one tenant, and
`GET /v1/me` reports it as `principal.principal_type = "api_key"`.

Each route checks, in order, the credential, the tenant and the role; a failed
check never reaches the handler:

- A missing, invalid or expired credential is `401 unauthenticated` with
  `WWW-Authenticate: Bearer`, and one message whatever check failed.
- `X-Tenant-Id` naming a tenant the credential does not hold is
  `403 tenant_forbidden`. A credential with several tenants must send it on
  admin routes, else `400 tenant_required`.
- A role below the route's lowest role is `403 forbidden`. Roles stack:
  `reader < editor < admin < owner`.
- Another tenant's resource reads as `404 not_found`, never `403`.

## Rate limits and deadlines

Each `/v1` call is charged, after the checks above pass, to a sliding
one-second window per principal: `HARBORRAG_HTTP_RATE_DEFAULT` (10) calls per
second, or `HARBORRAG_HTTP_RATE_CHAT` (2) for `POST /v1/chat/completions`.
A full window is `429 rate_limited` with an integer `Retry-After`. The windows
live in the capacity Redis when it is configured; when Redis is unreachable,
calls go through. The probes and `/metrics` are never limited.

A handler that runs past `HARBORRAG_API_REQUEST_TIMEOUT_SECONDS` (120) is
`504 deadline_exceeded`. A streamed chat body has its own
`HARBORRAG_API_STREAM_TIMEOUT_SECONDS` (600) and ends with an error frame.

## Writes and paging

| Mechanism | Rule |
| --- | --- |
| `ETag` | `W/"rev-<n>"` on reads and successful writes of projects and connections |
| `If-Match: <etag>` | The write fails with `412 precondition_failed` unless it matches |
| `If-Match: *` | Connection `PUT` updates only |
| `If-None-Match: *` | Project and connection `PUT` create only |
| `Idempotency-Key` | `POST /v1/chat/completions`: the same key and body within 24 h replays the stored result; the same key with another body is `422 validation_error`, and one still running is `409 already_running` |
| Paging | `?limit=` 1–100 (20 by default) and `?cursor=`; the body is `{"items": [...], "next_cursor": null}`. A cursor the server did not issue is `422` on field `cursor` |

## Metrics

`GET /metrics` answers only on `HARBORRAG_OPS_PORT` (9465), and only to
`Authorization: Bearer <HARBORRAG_METRICS_SCRAPE_TOKEN>`. Any other credential,
a user token included, is `401`; any other path on the ops port is `404`, and
the public port has no `/metrics`. The body is the API registry in the
Prometheus `text/plain; version=0.0.4` format: HTTP request counters and
durations by route template, build information and process metrics. Ingestion
counters come from the worker's own metrics port.

## Configuration

API process settings use the `HARBORRAG_` prefix.

| Variable | Default | Purpose |
| --- | --- | --- |
| `HARBORRAG_HOST` | `127.0.0.1` | Bind host for both sockets; containers override this to `0.0.0.0` |
| `HARBORRAG_PORT` | `8000` | Public port |
| `HARBORRAG_OPS_PORT` | `9465` | Ops port serving `/metrics`; must differ from the public port |
| `HARBORRAG_METRICS_SCRAPE_TOKEN` | unset | Bearer token Prometheus sends to `/metrics`; unset refuses every scrape |
| `HARBORRAG_ENV` | `dev` | `dev`, `staging` or `prod` process policy |
| `HARBORRAG_AUTH_MODE` | `none` | `none`, `hmac`, or `oidc` |
| `HARBORRAG_ALLOW_INSECURE_DEV` | `false` | Acknowledge disabled auth on a non-loopback development listener |
| `HARBORRAG_AUTH_SECRET` | unset | Shared HS256 secret |
| `HARBORRAG_AUTH_ISSUER` | `harborrag` | Required JWT issuer |
| `HARBORRAG_AUTH_AUDIENCE` | `harborrag-api` | Required JWT audience |
| `HARBORRAG_AUTH_USER_ID_CLAIM` | `sub` | Signed end-user claim used for session and memory ownership |
| `HARBORRAG_CORS_ORIGINS` | `[]` | JSON list of allowed browser origins |
| `HARBORRAG_DOCS_ENABLED` | `true` in dev | Enables Swagger at `/docs` and `/openapi.json` |
| `HARBORRAG_MAX_REQUEST_BODY_BYTES` | `1048576` | Largest body accepted before parsing |
| `HARBORRAG_HTTP_RATE_DEFAULT` | `10` | Calls per second per principal on every `/v1` route |
| `HARBORRAG_HTTP_RATE_CHAT` | `2` | Chat completions per second per principal |
| `HARBORRAG_API_CAPACITY_REDIS_URL` | unset in dev; required in prod | Redis for the rate windows and the chat capacity leases |
| `HARBORRAG_API_CAPACITY_ALLOW_INSECURE_REMOTE` | `false` | Development-only acknowledgement for remote plaintext Redis |
| `HARBORRAG_API_REQUESTS_PER_MINUTE` | `60` | Chat completions and legacy ingestion calls per principal per minute |
| `HARBORRAG_API_MAX_INFLIGHT_PER_PRINCIPAL` | `4` | Concurrent chat completions and legacy ingestion calls per principal |
| `HARBORRAG_API_REQUEST_TIMEOUT_SECONDS` | `120` | Deadline of every `/v1` handler |
| `HARBORRAG_API_STREAM_TIMEOUT_SECONDS` | `600` | Deadline of one streamed chat body |
| `HARBORRAG_CONTROL_DB_URL` | local SQLite runtime default | Control-plane SQLAlchemy URL |
| `HARBORRAG_SETTINGS_SEED_PATH` | `config/settings.yaml` | Seed file for runtime settings, read once when the tenant has no revisions; later differences log `config_yaml_drift` |
| `HARBORRAG_TEMPORAL_CONFIG_PATH` | `config/temporal.yaml` | Temporal connection, queues, retries, worker and workflow configuration |
| `HARBORRAG_TEMPORAL_TARGET` | value from YAML | Optional Temporal frontend override |
| `HARBORRAG_TEMPORAL_API_KEY` | unset | Secret API credential; keep it outside tracked YAML |
| `HARBORRAG_LOG_LEVEL` | `INFO` | HarborRAG namespace log level |

Credentialed CORS rejects wildcard origins. Swagger and `/openapi.json`
default to disabled in production unless an operator enables them.

## OpenAPI

In development:

```text
http://127.0.0.1:8000/docs
http://127.0.0.1:8000/openapi.json
```

Export the deterministic schema:

```bash
uv run python -m harborrag_app.api.export_openapi > openapi.json
```

## Package boundary

```text
api/
├── app.py, router.py, settings.py, serve.py
├── errors.py, dependency_failures.py, middleware.py, schemas.py
├── auth/                 # credentials, tenants, the ordered role guard
├── probes.py, readiness.py
├── ops.py, metrics.py    # the ops port and its Prometheus registry
├── rate_limit.py, deadline.py, capacity*.py
├── pagination.py, openapi.py
└── v1/                   # the public /v1 routes
    ├── admin/            # source types, connections and projects
    ├── chat/             # chat sessions and completions
    ├── ingestion/        # legacy ingestion routes
    └── me.py
```

This package may validate and serialize HTTP messages, authenticate principals
and enforce route roles, manage the API process and its middleware, and delegate
operations through `BaseAppService`. It must not instantiate connectors,
parsers, model clients, or repositories, implement ingestion state transitions,
expose Temporal SDK handles on the wire, or return provider exception details
or secrets.

Run its tests from the repository root:

```bash
pytest packages/harborrag-app/tests/test_api_*.py packages/harborrag-app/tests/test_export_openapi.py
```
