# Control Plane API

The `harborrag_app.api` package is HarborRAG's FastAPI boundary. It validates
HTTP input, authenticates callers, delegates workflow operations to the
transport-neutral application service, and maps results onto stable HTTP
responses. Connector execution, parsing, chunking, indexing, and Temporal
workflow rules remain outside this package.

Operational health and documentation routes remain under `/api/v1`. Stable
public ingestion, retrieval, chat, and agent resources are mounted under `/v1`.

For browser convenience, `/` and `/docs` redirect to `/api/v1/docs` while
documentation is enabled. When documentation is disabled, `/docs` remains
closed and `/` redirects to `/api/v1/health`.

## Install and run

Install the API dependencies from a source checkout:

```bash
uv sync --package harborrag-app --extra api
```

Start the application factory directly:

```bash
uv run uvicorn harborrag_app.api.app:create_fastapi_app \
  --factory \
  --host 127.0.0.1 \
  --port 8000
```

For the supported local Docker topology, use:

```bash
scripts/deployment/dev.sh up
curl --fail http://127.0.0.1:8000/api/v1/health
curl --fail -H "Authorization: Bearer $ADMIN_TOKEN" \
  http://127.0.0.1:8000/api/v1/metrics
```

The development helper starts the data services, Temporal, an ingestion
worker, and the API, then waits for its process health check. If those dependencies are
already running, `scripts/deployment/dev.sh api` starts only the API. API process
and authentication values belong in the ignored `env/.env.api`; embedding
credentials come from `env/.env.models` for synchronous vector retrieval.
Connector credentials remain isolated in the worker. See the repository
[deployment guide](../../../../../docs/developers/deployment/README.md) for
environment preparation and shutdown.

The local API container listens on `0.0.0.0` inside Docker but Compose publishes
port 8000 only on host loopback. Accordingly, the development environment file
sets `HARBORRAG_ALLOW_INSECURE_DEV=true` alongside `HARBORRAG_AUTH_MODE=none`.
Do not publish that configuration on a non-loopback host address; use HMAC auth
before exposing the API outside the trusted local development network.

## Composition modes

The application lifespan constructs one `AppService` and closes it on process
shutdown.

| Condition | Composition |
| --- | --- |
| `HARBORRAG_ENV=dev` without `HARBORRAG_CONTROL_DB_URL` | Development control-database stub plus the real Temporal client |
| A control-database URL is set | Migrated runtime control-plane repositories plus the real Temporal client |
| `HARBORRAG_ENV=prod` | Requires an explicit control-database URL and authenticated API mode |

Development mode changes only control-plane repository composition. Ingestion
requests still go to the configured Temporal service.

## HTTP routes

| Method | Path | Minimum role | Behavior |
| --- | --- | --- | --- |
The API serves two prefixes, and the distinction matters:

- **`/v1/...` is the public contract** - ingestion, retrieval, chat, agent, admin. This is
  what applications integrate against.
- **`/api/v1/...` is the operational surface** - health, readiness, metrics, and the
  console-facing project/source/activity/settings routes. Treat it as deployment
  plumbing, not an application API.

`/api/v1/docs` and `/api/v1/openapi.json` are served when `HARBORRAG_DOCS_ENABLED` is set;
`GET /` and `GET /docs` redirect there (or to `/api/v1/health` when docs are disabled).
The live schema is always authoritative - prefer it to this table.

### Public contract (`/v1`)

| Method | Path | Minimum role | Behavior |
| --- | --- | --- | --- |
| `POST` | `/v1/ingestions` | `editor` | Submit a durable ingestion task |
| `GET` | `/v1/ingestions` | `reader` | List tasks newest first, cursor-paginated |
| `GET` | `/v1/ingestions/{task_id}` | `reader` | Read Postgres-authoritative task progress |
| `GET` | `/v1/ingestions/{task_id}/documents` | `reader` | Read cursor-paginated document outcomes |
| `GET` | `/v1/ingestions/{task_id}/stream` | `reader` | Stream task progress as Server-Sent Events |
| `POST` | `/v1/ingestions/{task_id}/cancel` | `editor` | Request graceful cancellation |
| `POST` | `/v1/ingestions/{task_id}/retry-failures` | `editor` | Retry selected or all retryable failures |
| `GET` | `/v1/connections` | `reader` | List enabled connections submittable by `connection_id` |
| `POST` | `/v1/chat/sessions` | `reader` | Create a persisted chat session and greeting (`201`) |
| `POST` | `/v1/chat/completions` | `reader` | Retrieval-grounded JSON or SSE chat completion |
| `POST` | `/v1/agent/sessions` | `reader` | Create a persisted agent session and greeting (`201`) |
| `POST` | `/v1/agent/completions` | `reader` | Bounded multi-turn model/tool completion |
| `POST` | `/v1/agent/runs/{run_id}/resume` | `reader` | Continue a run that stopped before finishing |
| `POST` | `/v1/retrieval/vector` | `reader` | Dense, sparse, or hybrid vector search |
| `POST` | `/v1/retrieval/graph/triplets` | `reader` | Match subject-predicate-object records |
| `POST` | `/v1/retrieval/graph/paths` | `reader` | Find bounded paths between graph nodes |
| `POST` | `/v1/retrieval/graph/subgraphs` | `reader` | Expand a bounded graph neighborhood |
| `GET` | `/v1/admin/projections/{tenant}` | `admin` | Inspect projection state for a tenant |
| `DELETE` | `/v1/admin/projections/{tenant}` | `admin` | Drop a tenant's rebuildable projections |

### Operational surface (`/api/v1`)

| Method | Path | Minimum role | Behavior |
| --- | --- | --- | --- |
| `GET` | `/api/v1/health` | Public | Process liveness; does not probe dependencies |
| `GET` | `/api/v1/readyz` | Public | Dependency-aware readiness; `503` when runtime composition is unavailable |
| `GET` | `/api/v1/metrics` | `admin` | Prometheus API, Python runtime, and process metrics |
| `GET` | `/api/v1/metrics/ingestion` | `reader` | Ingestion-specific metrics |
| `GET` | `/api/v1/projects` | `reader` | List projects |
| `GET` | `/api/v1/projects/{project_id}` | `reader` | Read one project |
| `GET` | `/api/v1/sources` | `reader` | List configured sources |
| `POST` | `/api/v1/sources` | `editor` | Create a source |
| `GET` | `/api/v1/sources/{source_id}` | `reader` | Read one source |
| `PATCH` | `/api/v1/sources/{source_id}` | `editor` | Update a source |
| `DELETE` | `/api/v1/sources/{source_id}` | `editor` | Delete a source |
| `GET` | `/api/v1/activity` | `reader` | Recent activity feed |
| `GET` | `/api/v1/settings` | `reader` | Effective non-secret settings |

### Deprecated

These predate the `/v1` contract, still appear in the generated schema, and will be
removed. Use the `/v1/ingestions*` routes instead.

| Method | Path | Minimum role |
| --- | --- | --- |
| `GET` | `/api/v1/diagnostics` | `admin` |
| `POST` | `/api/v1/ingestions` | `editor` |
| `GET` | `/api/v1/ingestions/{run_id}` | `reader` |
| `GET` | `/api/v1/ingestions/{run_id}/result` | `reader` |
| `POST` | `/api/v1/ingestions/{run_id}/actions` | `editor` |

The API authenticates the principal and accepts tenant as one explicit,
top-level request field. It defaults to `DEFAULT`; callers cannot bypass tenant
isolation through retrieval filters.

### Generate a chat completion

Chat uses the `chat` section of `config/models.yaml`, resolves credentials from
`env/.env.models`, and retrieves tenant-scoped evidence before calling the
model. Callers may choose per-request graph observation and streaming, but
cannot submit a system prompt, provider
credentials, model overrides, custom tools, or adapter-specific parameters.

```bash
curl --fail-with-body \
  --request POST \
  --header 'Content-Type: application/json' \
  --data '{"tenant":"DEFAULT"}' \
  http://127.0.0.1:8000/v1/chat/sessions

curl --fail-with-body --request POST \
  --header 'Content-Type: application/json' \
  --data '{"session_id":"session-...","prompt":"What is HarborRAG?"}' \
  http://127.0.0.1:8000/v1/chat/completions
```

The session response contains a generated `session_id` and greeting. The completion POST
requires that session ID and a prompt in its JSON body. `stream=true` changes the response to
SSE. The two latest PostgreSQL-backed turns are recalled under the tenant,
authenticated principal, and session identity. Requests are marked sensitive
so model-response caching remains disabled. Completion endpoints accept POST
only, keeping prompts and other sensitive content out of request URLs and access logs.

### Run a multi-turn agent

The agent uses the same model and memory identity but can execute multiple
model/tool hops before synthesizing an answer. Only runtime-owned read tools
are exposed. `graph_search` controls whether graph triplet, path, and subgraph
tools are available, and `max_steps` bounds model/tool rounds from 1 to 8.

```bash
curl --fail-with-body \
  --request POST \
  --header 'Content-Type: application/json' \
  --data '{"tenant":"DEFAULT"}' \
  http://127.0.0.1:8000/v1/agent/sessions

curl --fail-with-body --request POST \
  --header 'Content-Type: application/json' \
  --data '{"session_id":"session-...","prompt":"Connect this release policy to its owner.","graph_search":true,"max_steps":4}' \
  http://127.0.0.1:8000/v1/agent/completions
```

The POST returns a `session_id` and greeting. Agent completions return
aggregate token usage, turn count, and a safe tool trace containing tool names
and success status. Raw tool arguments and results are not returned.

The former `POST /v1/retrieval/search` route has been removed. Retrieval
operations are intentionally not exposed as GET: query text and nested filters
belong in a validated request body and should not be copied into URLs, access
logs, browser history, or intermediary cache keys. A future GET endpoint should
represent a persisted retrieval resource; searches are not persisted today.

### Retrieve evidence

Vector search has one contract for both simple and advanced callers. Omitting
controls uses hybrid retrieval; advanced callers may select `dense`, `sparse`,
or `hybrid`, apply metadata filters, observe graph context, set a score
threshold, and control content or metadata projection.

`filters` is optional. Omit it for an unfiltered search; when supplied, use
real indexed metadata keys such as `{"category": "architecture"}`. The
`additionalProp1` names shown by generic JSON-object schema renderers are
placeholders, not fields recognized by HarborRAG.

This is a synchronous `POST`: a successful response contains at most `top_k`
ranked results immediately. It never returns an ingestion-style `task_id` and
does not require a follow-up GET. The optional `request_id` in the response is
only a trace/correlation identifier. A retrieval GET should be added only if
search results become persisted resources.

```bash
curl --fail-with-body \
  --request POST \
  --header 'Content-Type: application/json' \
  --data '{
    "query": "publication policy",
    "tenant": "DEFAULT",
    "top_k": 10,
    "lane": "hybrid",
    "filters": {"category": "architecture"},
    "observe_graph": true,
    "score_threshold": 0.2,
    "include_content": true,
    "include_metadata": true
  }' \
  http://127.0.0.1:8000/v1/retrieval/vector
```

Graph operations use portable graph records rather than FalkorDB query syntax.
Triplets accept any combination containing at least one of `subject`,
`predicate`, or `object`; paths require distinct start and end nodes; subgraph
expansion requires one start node. Path depth is capped at 8 and all graph
result counts are capped at 100.

### Submit ingestion

```bash
curl --fail-with-body \
  --request POST \
  --header 'Content-Type: application/json' \
  --header 'Idempotency-Key: local-release-notes-2026-08' \
  --data '{
    "connection_id": "harborrag-workspace",
    "tenant": "DEFAULT",
    "mode": "incremental"
  }' \
  http://127.0.0.1:8000/v1/ingestions
```

The application generates a public UUIDv4 task ID. Before Temporal is
called, it persists the source scope and a `PENDING` task in Postgres. Reusing
an idempotency key with the same public request returns the existing task;
reusing it for a different request returns a conflict.

Task paths intentionally continue to accept opaque strings, so tasks created
by older versions with `ing_...` or `retry_...` identifiers remain queryable.

The connection must be enabled in the same connector configuration used by the
worker. Provider, scope, paths, attachment/comment policy, limits, and
credentials come from `config/connectors.yaml`; the API does not duplicate or
override them. The API resolves only connector identity and processing
fingerprints; connector calls and model execution remain worker activities.

`mode` defaults to `incremental`. It discovers the configured scope and skips
unchanged documents. `force` fetches and evaluates unchanged documents again,
but it does not delete data, force a new deterministic document version, or
rebuild an already-active projection merely because its storage collection was
renamed. See [Ingestion modes](../../../../../docs/users/ingestion-modes.md) for
the complete behavior and reindex guidance.

### List connections and tasks

```bash
curl --fail-with-body http://127.0.0.1:8000/v1/connections

curl --fail-with-body   'http://127.0.0.1:8000/v1/ingestions?status=RUNNING&limit=25'
```

`/v1/connections` returns the `connection_id` and `source_type` of every
connection enabled in `config/connectors.yaml`, which is the only place those
names are defined. Nothing else about a connector is exposed: settings,
environment references, and credential references stay server-side. A
definition the catalog could not parse is omitted rather than failing the
listing; submitting it still reports its own configuration error.

`/v1/ingestions` lists tasks newest submission first and pages with an opaque
`cursor`, exactly like `/v1/ingestions/{task_id}/documents`. Each item carries
the same fields as the single-task response. `tenant` narrows the listing to
one tenant the caller may read; omitting it reads every tenant in the caller's
own scope, so a tenant-scoped token never sees another tenant's tasks and an
out-of-scope `tenant` is rejected rather than answered with an empty page.

### Inspect and control a task

```bash
curl --fail-with-body \
  http://127.0.0.1:8000/v1/ingestions/2f47e0c9-398b-4b87-ae72-c6778f08a18a

curl --fail-with-body \
  'http://127.0.0.1:8000/v1/ingestions/2f47e0c9-398b-4b87-ae72-c6778f08a18a/documents?limit=50'

curl --fail-with-body \
  --request POST \
  http://127.0.0.1:8000/v1/ingestions/2f47e0c9-398b-4b87-ae72-c6778f08a18a/cancel
```

Cancellation is asynchronous and takes effect at safe workflow boundaries.
Published document versions remain active. The retry endpoint creates a new
UUIDv4 task and starts at the earliest reusable durable artifact; it does
not refetch a source when a raw or later artifact exists.

## Authentication and roles

`HARBORRAG_AUTH_MODE` supports:

- `none`: local development only; assigns an implicit `owner` principal;
- `hmac`: verifies HS256 bearer JWTs using `HARBORRAG_AUTH_SECRET`;
- `oidc`: reserved but not implemented; application creation fails fast.

Production refuses `auth_mode=none`. HMAC secrets must contain at least 32
UTF-8 bytes. Tokens must contain `sub`, `role`, `tenants`, `iat`, `exp`, `iss`,
and `aud` claims. `tenants` is a non-empty list of tenant identifiers the
principal may access; a global role does not grant access to other tenants.
The default issuer is `harborrag`, the default audience is `harborrag-api`, and
the role order is:

```text
reader < editor < admin < owner
```

Send authenticated requests with:

```http
Authorization: Bearer JWT
```

The liveness endpoint intentionally remains unauthenticated. Prometheus metrics
require an admin bearer token at `/api/v1/metrics`; configure a protected scrape
credential or a separate operational listener when the API is
not running on a trusted service network.

## Configuration

API process settings use the `HARBORRAG_` prefix.

| Variable | Default | Purpose |
| --- | --- | --- |
| `HARBORRAG_HOST` | `127.0.0.1` | Uvicorn bind host; containers override this to `0.0.0.0`, where disabled auth requires an explicit insecure-development opt-in |
| `HARBORRAG_PORT` | `8000` | Uvicorn bind port used by the container command |
| `HARBORRAG_ENV` | `dev` | `dev` or `prod` process policy |
| `HARBORRAG_AUTH_MODE` | `none` | `none`, `hmac`, or `oidc` |
| `HARBORRAG_ALLOW_INSECURE_DEV` | `false` | Explicitly acknowledge disabled auth on a non-loopback development listener |
| `HARBORRAG_AUTH_SECRET` | unset | Shared HS256 secret |
| `HARBORRAG_AUTH_ISSUER` | `harborrag` | Required JWT issuer |
| `HARBORRAG_AUTH_AUDIENCE` | `harborrag-api` | Required JWT audience |
| `HARBORRAG_CORS_ORIGINS` | `[]` | JSON list of allowed browser origins |
| `HARBORRAG_DOCS_ENABLED` | `true` in dev | Enables Swagger and the live OpenAPI route |
| `HARBORRAG_MAX_REQUEST_BODY_BYTES` | `1048576` | Maximum request body accepted before JSON parsing |
| `HARBORRAG_API_CAPACITY_REDIS_URL` | unset in dev; required in prod | Redis backend for cross-replica principal limits |
| `HARBORRAG_API_CAPACITY_ALLOW_INSECURE_REMOTE` | `false` | Development-only acknowledgement for remote plaintext Redis |
| `HARBORRAG_API_REQUESTS_PER_MINUTE` | `60` | Maximum expensive requests per principal per minute |
| `HARBORRAG_API_MAX_INFLIGHT_PER_PRINCIPAL` | `4` | Concurrent expensive request limit per principal |
| `HARBORRAG_API_REQUEST_TIMEOUT_SECONDS` | `120` | Server-owned wall-clock deadline for expensive requests |
| `HARBORRAG_CONTROL_DB_URL` | local SQLite runtime default | Control-plane SQLAlchemy URL |
| `HARBORRAG_INGESTION_TENANT_ID` | `DEFAULT` | Fallback tenant for non-HTTP runtime operations |
| `HARBORRAG_TEMPORAL_CONFIG_PATH` | `config/temporal.yaml` | Temporal connection, queues, retries, worker, ingestion, health, and workflow configuration |
| `HARBORRAG_TEMPORAL_TARGET` | value from YAML | Optional Temporal frontend override |
| `HARBORRAG_TEMPORAL_NAMESPACE` | value from YAML | Optional Temporal namespace override |
| `HARBORRAG_TEMPORAL_TLS` | value from YAML | Optional TLS override for a managed Temporal endpoint |
| `HARBORRAG_TEMPORAL_API_KEY` | unset | Secret API credential; keep it outside tracked YAML |
| `HARBORRAG_TEMPORAL_ALLOW_INSECURE_REMOTE` | `false` | Explicit opt-in for trusted plaintext remote targets |
| `HARBORRAG_TEMPORAL_INGESTION_BATCH_SIZE` | `200` | Default documents per `SourceBatchWorkflow` child; a CLI/API caller may override it per submission |
| `HARBORRAG_TEMPORAL_INGESTION_DOCUMENT_CONCURRENCY` | `8` | Default concurrent `DocumentIngestionWorkflow` children per wave; a CLI/API caller may override it per submission |
| `HARBORRAG_LOG_LEVEL` | `INFO` | HarborRAG namespace log level |

Credentialed CORS rejects wildcard origins. Swagger and the live OpenAPI route
default to disabled in production unless an operator explicitly enables them.

INFO logs report ingestion submission, discovery, document outcomes, failures,
and finalization. DEBUG additionally reports every Temporal activity boundary
and duration. Records include logger namespace, Python module, function, and
source line while omitting credentials and document/model content. See the
[troubleshooting guide](../../../../../docs/users/troubleshooting/README.md#an-accepted-ingestion-appears-to-do-nothing)
when an accepted task appears idle.

## Errors and request tracing

Every non-success response uses one error envelope:

```json
{
  "error": {
    "code": "harbor_validation_error",
    "message": "Request validation failed",
    "details": {},
    "trace_id": "request-123"
  }
}
```

Send `X-Request-Id` to correlate a request with logs. The API echoes that value
on the response; when it is absent, middleware generates one. Unexpected
exceptions return a generic error while the complete traceback remains in
process logs.

## OpenAPI contract

In development:

```text
http://127.0.0.1:8000/api/v1/docs
http://127.0.0.1:8000/api/v1/openapi.json
```

Export the deterministic contract without starting a server:

```bash
uv run python -m harborrag_app.api.export_openapi > openapi.json
```

## Package boundary

The package is organized by responsibility:

```text
api/
├── app.py, router.py, settings.py, middleware.py, metrics.py
├── auth/                 # authentication and role dependencies
├── routes/               # operational surface: health, metrics, projects,
│                         #   sources, activity, settings, deprecated routes
├── schemas.py            # contracts shared by API features
└── v1/                   # the public /v1 contract
    ├── admin/            # projection inspection and teardown
    ├── agent/            # agent sessions, completions, run resume
    ├── chat/             # chat sessions and completions
    ├── connections/      # submittable connection listing
    ├── ingestion/        # ingestion routes, schemas, and command mapping
    └── retrieval/        # retrieval routes, schemas, and service dependency
```

This package may:

- validate and serialize HTTP messages;
- authenticate principals and enforce route roles;
- manage API process lifecycle and middleware;
- delegate operations through `BaseAppService`.

It must not:

- instantiate connectors, parsers, model clients, or repositories;
- implement ingestion state transitions;
- expose Temporal SDK handles on the wire;
- return provider exception details or secrets.

Run its tests from the repository root:

```bash
pytest packages/harborrag-app/tests/test_api_*.py
```
