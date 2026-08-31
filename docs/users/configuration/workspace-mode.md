# Tenant and Workspace Status

HarborRAG supports explicit tenant-scoped ingestion, retrieval, chat, model
policy, and persistence. It does **not** currently provide a local “workspace
mode” that discovers project configuration, creates a `.harbor` directory, or
switches an entire runtime stack by workspace name.

Use a tenant ID to select an isolation boundary. Do not treat it as a workspace
profile, a deployment selector, or proof that a caller is authorized.

## Capability summary

| Capability | Status | Meaning |
| --- | --- | --- |
| Explicit tenant scope in the CLI, SDK, HTTP API, and MCP tools | Implemented | Pass the same tenant ID at every entry point that participates in one workload. |
| Principal and tenant operation context | Implemented | `AccessContext` carries the caller and tenant through runtime and repository operations. |
| API roles and tenant grants | Implemented | Authenticated requests are checked against the tenant IDs granted to the principal. Development mode can run without authentication. |
| Tenant-specific MCP tool policy | Implemented | `config/mcp.yaml` can override tool enablement, defaults, and limits by tenant. |
| Tenant-isolated vector and graph retrieval | Implemented | Qdrant and FalkorDB use different isolation mechanisms described below. |
| Tenant-isolated object and workflow state storage | Implemented | Object keys, workflow state, checkpoints, and leases include a tenant namespace. |
| Tenant-owned control-plane records | Implemented | Projects, sources, providers, members, activity, jobs, and settings carry tenant identity. |
| Intra-tenant document permissions | Not implemented | A principal with access to a tenant can retrieve all active evidence in that tenant. |
| OIDC/JWKS authentication | Not implemented | `oidc` is a reserved API mode; the current application fails closed if it is selected. |
| Auto-discovered `.harbor` workspace profiles | Not implemented | Configuration files and environment variables must be selected explicitly. |
| Automatic per-tenant connector, parser, model, or credential selection | Not implemented | Process configuration is shared unless the embedding application composes separate runtimes. |
| Automatic tenant provisioning or one runtime stack per tenant | Not implemented | Deployment and tenant lifecycle remain operator responsibilities. |
| Atomic tenant rename or deletion | Not implemented | Data migration and retirement must cover every persistence family explicitly. |

## Tenant, principal, project, and workspace

These terms are related but not interchangeable:

- A **tenant** is the top-level data and policy boundary. Its `tenant_id` is
  carried across ingestion, retrieval, model calls, and repositories.
- A **principal** is the user, service, or trusted runtime performing an
  operation. `AccessContext` binds one `principal_id` to one `tenant_id`.
- A **project** is a tenant-owned control-plane grouping for sources and
  documents. It does not replace the tenant boundary.
- A **workspace** is control-plane terminology for tenant-owned settings,
  members, providers, and projects. Those records do not create a local
  configuration profile or switch processes, credentials, and backends.

## Choose a tenant ID

`harborrag_core.schemas.ids.TenantId` is the JSON-compatible identifier shared
by HarborRAG storage contracts. HarborRAG's Pydantic contracts reject empty
values and strip leading and trailing whitespace. Control-plane domain records
reject whitespace, and the runtime environment validator is stricter still.
Public API and Qdrant paths accept up to 128 characters, while current SQL
workflow-state schemas use 64-character tenant columns. For compatibility
across every built-in backend, use a portable identifier with these properties:

- 1–64 characters (128 is accepted only on surfaces that document that limit);
- starts with an ASCII letter or digit;
- contains only letters, digits, `.`, `_`, or `-`;
- contains no secrets or personally identifiable information.

The portable form is equivalent to this regular expression:

```text
^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$
```

For example, use `engineering`, `tenant-7`, or `acme.prod`. Tenant comparisons
are exact, so use one canonical spelling and treat IDs as case-sensitive. A
tenant rename does not migrate existing records, vector collections, graph
nodes, chat sessions, or model-policy state.

`DEFAULT` is convenient for local development. Use explicit, stable IDs when
testing isolation or running a shared environment.

Choose the ID in an identity or deployment control plane, not from a request's
free-form organization name. Keep a registry that records the canonical ID,
display name, owners, permitted principals, environment, retention policy, and
lifecycle state. HarborRAG does not currently maintain that registry for you.

## Configure the runtime fallback

The runtime reads a fallback tenant for non-HTTP operations that do not receive
one explicitly:

```bash
export HARBORRAG_INGESTION_TENANT_ID=tenant-1
```

The default is `DEFAULT`. This environment variable is a fallback, not a global
workspace selector: CLI commands, API requests, SDK calls, and MCP tools can
supply their own tenant. Pass the tenant explicitly whenever isolation matters.

`EngineConfig.tenant` is currently a diagnostics label. It does not construct
an `AccessContext`, populate model request metadata, or override the tenant at
an application boundary. See [Engine and ingestion runtime
configuration](config-file-reference.md).

### Tenant selection by surface

There is no single implicit precedence chain shared by every interface. Each
entry point owns its tenant field:

| Surface | Tenant input | Default | Authorization behavior |
| --- | --- | --- | --- |
| CLI ingestion, retrieval, and chat | `--tenant` | `DEFAULT` | The local CLI trusts the operator; it does not validate a bearer-token grant. |
| Python SDK ingestion and retrieval | `AccessContext.tenant_id` | None | Required. The embedding application must authenticate and authorize the principal before constructing the context. |
| Stable `/v1` HTTP API | Top-level JSON field `tenant` | `DEFAULT` | The API checks the authenticated principal's role and tenant grants. |
| Legacy ingestion API | Top-level JSON field `tenant_id` | Route-specific | Retained for compatibility; prefer stable `/v1` routes for new clients. |
| MCP retrieval tools | Top-level argument `tenant_id` | None | Required. Authenticated transports check the owner token's tenant grants. |
| Runtime composition fallback | `HARBORRAG_INGESTION_TENANT_ID` or `runtime.ingestion_tenant_id` | `DEFAULT` | Used only where a runtime path does not already receive an explicit tenant. |
| Engine diagnostics | `EngineConfig.tenant` | `default` | Diagnostic label only; it does not override operational scope. |

An explicit request value wins because it is carried in that request's
`AccessContext` or public request schema. Changing an environment fallback does
not rewrite a CLI argument, API body, SDK context, durable workflow input, or
previously stored data.

### Explicit SDK configuration file

The SDK can load a named YAML file when your application passes its path:

```yaml
execution_mode: temporal
discover_plugins: false
runtime:
  ingestion_tenant_id: tenant-1
  connector_config_path: config/connectors.yaml
  parser_config_path: config/parsers.yaml
  model_config_path: config/models.yaml
  temporal_target: localhost:7233
```

```python
from harborrag_runtime.sdk import HarborRAG

harbor = HarborRAG.from_config("config/harborrag.example.yaml")
```

This is explicit file loading, not workspace discovery. HarborRAG does not walk
parent directories, choose a file based on the current directory, merge a
`.harbor` profile, or activate a tenant because a filename matches it. Protect
secrets in the process environment or a secret manager rather than putting them
in this YAML file.

## Use one tenant across entry points

### CLI

Pass `--tenant` to each command in a workflow:

```bash
harborrag ingest start \
  --tenant tenant-1 \
  --connector harborrag-workspace \
  --wait

harborrag retrieve "deployment requirements" \
  --tenant tenant-1 \
  --top-k 5 \
  --json

harborrag chat "Summarize the deployment requirements." \
  --tenant tenant-1 \
  --json
```

Retrieval filters cannot contain `tenant_id`. The top-level `--tenant` option
is the only tenant scope for the command, which prevents a metadata filter from
overriding the isolation boundary. See the [CLI reference](../cli-reference/README.md).

The connector name selects a process-configured connector definition; it does
not select a tenant. The same connector can be invoked for different tenants,
but each invocation creates separate tenant-owned source state and projections.
Ingestion control commands such as `status`, `wait`, `pause`, and `cancel` use
the durable run ID created by `ingest start`; preserve that ID with the tenant
and source identity in operator records.

### Python SDK and repositories

Create `AccessContext` at the authenticated application boundary and reuse it.
When a repository operation needs durable storage metadata, wrap that access
context in `StorageOperationContext`:

```python
from harborrag_core.schemas.ids import TenantId
from harborrag_core.schemas.storage import StorageOperationContext
from harborrag_core.security import AccessContext

access = AccessContext(
    principal_id="user-1",
    tenant_id=TenantId("tenant-1"),
)

storage_context = StorageOperationContext.for_access(
    access,
    operation_kind="vector.search",
    idempotency_key="request-123",
)
```

SDK ingestion and retrieval requests require the access context. Reusing one
object makes accidental spelling drift less likely:

```python
from harborrag_runtime.sdk import (
    HarborRAG,
    HarborRAGConfig,
    IngestionRequest,
    RetrievalRequest,
)

async def index_and_search() -> None:
    async with HarborRAG(HarborRAGConfig()) as harbor:
        await harbor.ingestion.run(
            IngestionRequest(
                access=access,
                connector_name="harborrag-workspace",
            )
        )
        response = await harbor.retrieval.search(
            RetrievalRequest(
                access=access,
                query="deployment requirements",
                top_k=5,
            )
        )
        print(response.results)
```

`AccessContext` is an enforcement carrier, not an authenticator. The SDK cannot
tell whether `principal_id="user-1"` came from a verified session or untrusted
input. An embedding service must authenticate the caller, authorize that caller
for `tenant-1`, and only then construct the context.

`StorageOperationContext.tenant_id` exposes the enforced tenant to repository
implementations. It can also carry an operation kind, idempotency key, document
ID, and document-version ID.

Trusted background work can use
`StorageOperationContext.system("tenant-1", ...)`, which assigns the explicit
runtime principal. Do not use that shortcut for an end-user request because it
replaces the authenticated principal.

### HTTP API

Public API request bodies use the top-level field name `tenant`:

```bash
curl --fail-with-body \
  --request POST \
  --header 'Content-Type: application/json' \
  --header "Authorization: Bearer $HARBORRAG_API_TOKEN" \
  --data '{
    "query": "deployment requirements",
    "tenant": "tenant-1",
    "top_k": 5,
    "lane": "hybrid"
  }' \
  http://127.0.0.1:8000/v1/retrieval/vector
```

With HMAC authentication enabled, the bearer token contains the principal's
role and a `tenants` claim. The API rejects a request whose `tenant` is not in
that grant, even if the principal has a high role. A `*` grant is unrestricted
and should be reserved for trusted administrative callers.

`HARBORRAG_AUTH_MODE=none` is a development-only mode that treats every request
as an owner with unrestricted tenant access. Production configuration refuses
to start with authentication disabled. A tenant ID is a routing label, not a
credential; never authorize a request from its claimed tenant alone.

#### Configure API authentication

The current authenticated API mode is HMAC-signed HS256 JWT. Important process
settings are:

| Variable | Requirement or default | Purpose |
| --- | --- | --- |
| `HARBORRAG_ENV` | `dev`; set `prod` for production | Enables production fail-closed checks. |
| `HARBORRAG_AUTH_MODE` | `none`; use `hmac` for an exposed service | Selects the token verifier. `oidc` is reserved but not implemented. |
| `HARBORRAG_AUTH_SECRET` | Required for HMAC; at least 32 UTF-8 bytes | Verifies HS256 signatures. Load it from protected deployment configuration. |
| `HARBORRAG_AUTH_ISSUER` | `harborrag` | Required `iss` claim. |
| `HARBORRAG_AUTH_AUDIENCE` | `harborrag-api` | Required `aud` claim. |
| `HARBORRAG_AUTH_MAX_TOKEN_LIFETIME_SECONDS` | `3600` | Maximum accepted difference between `iat` and `exp`. |
| `HARBORRAG_AUTH_CLOCK_SKEW_SECONDS` | `30` | Permitted verification clock skew. |

Do not commit the HMAC secret or use it as a tenant identifier. Production also
requires the other production process dependencies documented in [Engine and
ingestion runtime configuration](config-file-reference.md); authentication
settings alone are not a complete deployment configuration.

A verified token must contain claims equivalent to this payload:

```json
{
  "sub": "service-release-bot",
  "role": "editor",
  "tenants": ["tenant-1"],
  "iat": 1787558400,
  "exp": 1787562000,
  "iss": "harborrag",
  "aud": "harborrag-api"
}
```

The `tenants` value must be a non-empty list of non-empty strings. Matching is
exact and case-sensitive. `"*"` grants every tenant; a high role without a
matching tenant grant is still rejected.

The API role ladder is `reader < editor < admin < owner`:

| Role | Tenant-relevant access |
| --- | --- |
| `reader` | Retrieval, chat, agent, and read-only task/control-plane routes within granted tenants. |
| `editor` | Reader access plus ingestion submission and other editor mutations. |
| `admin` | Editor access plus administrative routes that require the admin level. |
| `owner` | Highest API role. MCP transports currently require this role independently. |

Authorization failures are deliberately distinguishable where safe:

- missing, expired, malformed, or incorrectly signed credentials return `401`;
- an insufficient role or disallowed requested tenant returns `403`;
- looking up an ingestion task outside the principal's tenants returns `404`,
  hiding whether that task ID exists;
- an invalid tenant shape in a stable `/v1` request returns validation error
  `422` before work starts.

With authentication disabled, the listener is restricted to loopback unless an
operator explicitly sets `HARBORRAG_ALLOW_INSECURE_DEV=true`. That flag is an
acknowledgement for development, not a production authentication mechanism.

### MCP

Every retrieval tool requires `tenant_id` as a top-level argument:

```python
result = await server.call_tool(
    "vector_search",
    {
        "query": "deployment requirements",
        "tenant_id": "tenant-1",
        "top_k": 5,
    },
    principal_id="user-1",
)
```

Authenticated MCP transports require an owner token and verify the requested
tenant against its `tenants` claim. As with CLI retrieval, `tenant_id` cannot be
duplicated inside `filters`.

Tenant entries in `config/mcp.yaml` change the effective tool catalog and
policy; they do not create tenants or move data:

```yaml
tenants:
  tenant-1:
    tools:
      vector_search:
        defaults:
          top_k: 8
      graph_subgraph_search:
        enabled: false
```

See [MCP Setup and Integration](../detailed-guides/mcp-server/setup-and-integration.md)
for transport authentication and the tenant-effective tool catalog.

MCP configuration resolves in this order for each tool call:

1. compiled tool schema and safety ceilings;
2. global `policy` and global tool settings from `config/mcp.yaml`;
3. the matching `tenants.<tenant_id>.tools` override;
4. environment overrides applied after the file;
5. request arguments, checked against the resolved schema and limits.

Configuration validation fails closed. Unknown tenants are allowed to use the
global policy, but unknown tool names or fields are rejected. A configuration
cannot provide a default for `tenant_id` or any other required tool argument,
so the caller must always make tenant intent explicit. A tenant override can
reduce limits or disable a tool; it cannot raise a compiled safety ceiling.

Direct `McpServer.call_tool(...)` use is a trusted in-process boundary. Its
`principal_id` parameter labels audit records but does not authenticate the
caller. Authenticated FastMCP or HTTP paths perform owner-role and tenant grant
checks before dispatching the same tool. Local stdio without a token remains a
trusted process boundary. Applications embedding the server directly must
implement equivalent authorization themselves.

## Context propagation contracts

The safest design has one decision point at the application boundary and no
later tenant re-selection:

```text
verified principal + requested tenant
                |
                v
          AccessContext
           /         \
          v           v
StorageOperationContext   ModelRequestMetadata
          |                    |
          v                    v
SQL / Qdrant / graph /    cache / budget /
object / state stores     telemetry metadata
```

### `AccessContext`

`AccessContext` is intentionally small:

| Field | Contract |
| --- | --- |
| `principal_id` | Required non-empty caller identity, up to 255 characters. |
| `tenant_id` | Required `TenantId` used as the operational data boundary. |

Create one after authentication and tenant authorization. Pass the same context
through SDK ingestion, vector retrieval, and graph retrieval. Do not reconstruct
it from arbitrary document metadata, a connector payload, or model output.

### `StorageOperationContext`

Repository methods receive a `StorageOperationContext`, which retains
`AccessContext` and adds durable operation metadata:

| Field | Purpose |
| --- | --- |
| `access` | Authenticated or trusted principal and enforced tenant. |
| `operation_kind` | Bounded operation label, such as `vector.search`; defaults to `unspecified`. |
| `idempotency_key` | Optional replay/deduplication identity for a durable operation. |
| `document_id` | Optional document correlation identity. |
| `document_version_id` | Optional immutable-version correlation identity. |

`StorageOperationContext.for_access(...)` preserves the end-user principal.
`StorageOperationContext.system(...)` creates the fixed
`harborrag-runtime` principal for trusted background execution. Repository
records that already contain a tenant must match `context.tenant_id`; adapters
reject mismatches rather than silently rewriting record ownership.

### Model request metadata

`ModelRequestMetadata` carries correlation fields such as `request_id`,
`trace_id`, `tenant_id`, `user_id`, `workflow_id`, collection, and pipeline
stage. It is deliberately separate from `AccessContext` because model provider
metadata is not an authorization object.

When calling model adapters directly, copy the authorized tenant explicitly:

```python
from harborrag_core.models import ModelRequestMetadata

model_metadata = ModelRequestMetadata(
    request_id="request-123",
    trace_id="trace-123",
    tenant_id=str(access.tenant_id),
    user_id=access.principal_id,
)
```

Do not derive tenant authorization from model metadata on the return path.
Provider headers and telemetry may propagate a sanitized or hashed tenant
identifier, but the original `AccessContext` remains the data-access authority.

## How data isolation works

Tenant isolation is enforced at multiple layers:

| Layer | Isolation behavior |
| --- | --- |
| Control-plane repositories | Tenant-owned records carry `tenant_id`; repository contracts expose tenant-scope filters for data access. |
| Canonical SQL repositories | Documents, versions, chunks, events, and outbox records store tenant identity and use it in reads and ownership checks. |
| Qdrant | Each tenant receives a physically separate vector collection. `tenant_id` is deliberately not stored as a payload filter. |
| FalkorDB | Tenants share one graph. `tenant_id` participates in node identity, uniqueness constraints, writes, and retrieval filters. |
| Filesystem and S3 object stores | Logical object keys are placed below an opaque SHA-256-derived tenant prefix. |
| SQL and Redis workflow state | Workflow state, checkpoints, leases, and fencing counters include tenant in keys or ownership checks. |
| Temporal ingestion | Tenant identity is persisted in workflow/source inputs so retries and resumed activities keep the original scope. |
| Chat memory | Sessions and turns are keyed by tenant, principal, and session ID. |
| Model cache, singleflight, and budgets | Model request metadata supplies `tenant_id`; configured policies can require it and partition state by tenant. |
| MCP tool policy | The tenant chooses effective enablement, defaults, and limits after transport authorization; the runtime context still provides data isolation. |

Qdrant's physical partition and FalkorDB's logical partition are both required;
one is not a substitute for the other. See [Projection and rebuild
architecture](../../developers/architecture/projection-rebuild.md#the-tenant-spine)
for the storage design.

### Qdrant collection isolation

The Qdrant adapter derives a physical collection name from the optional process
prefix, tenant, and logical index name:

```text
{qdrant_collection_prefix}{tenant_id}_{logical_index}
```

Both tenant and logical index must use 1–128 ASCII letters, digits, `.`, `_`, or
`-`, beginning with a letter or digit. The tenant selects the collection before
metadata filters are built. Therefore, `tenant_id` is intentionally absent from
the Qdrant payload and cannot be used as a user-supplied filter to jump between
collections.

Physical separation simplifies data-path isolation but affects lifecycle work:
each tenant has separate collection creation, schema validation, inventory,
rebuild, and deletion. Changing `HARBORRAG_QDRANT_COLLECTION_PREFIX` points the
runtime at different physical names; it does not rename or repopulate existing
collections.

### FalkorDB graph isolation

FalkorDB uses one configured graph, so tenant identity is part of every relevant
node and relationship operation. The graph adapter includes `tenant_id` in
merge identities, uniqueness constraints, traversal predicates, administrative
counts, cleanup, and projection deletion. This is necessary even when a node key
is deterministic: two tenants can ingest identical content and produce the same
document-version or chunk key.

Graph queries must continue to use repository methods that receive
`StorageOperationContext`. Do not run application-supplied Cypher directly
against the shared graph, because doing so bypasses those tenant predicates.

### Object-store isolation

Filesystem and S3 adapters map a logical object key to an opaque physical
namespace:

```text
.harborrag/tenants/{sha256(tenant_id)}/{logical_key}
```

Listing, reading, writing, presigning, and deletion calculate the prefix from
`context.tenant_id`. The digest makes the prefix path-safe and avoids placing a
raw tenant label in the object key; it is namespace derivation, not encryption.
Bucket policy and infrastructure credentials must still prevent direct access
that bypasses HarborRAG.

### Relational and workflow-state isolation

Canonical and control-plane records store `tenant_id` in indexed columns,
composite identities, or explicit scope filters. Workflow-state adapters use
tenant in state, checkpoint, lease, and fencing identities. Records supplied to
state repositories are checked against the operation context, so a record for
tenant A cannot be written with tenant B's context.

Database credentials are normally shared by the process. Row-level tenant
columns are therefore an application-enforced boundary, not a claim that every
tenant has a separate database account or schema. Operators needing physical
database isolation must compose separate deployments or adapters.

### Model-policy isolation

Model request metadata is a separate contract from `AccessContext`. When using
model adapters directly, propagate the same tenant into
`ModelRequestMetadata.tenant_id`. Tenant-isolated cache or budget policy may
reject or bypass a request when it is missing. See [Model
configuration](model-config.md).

The exact missing-tenant behavior depends on the policy:

- Harbor response caching bypasses the cache when `require_tenant_id` is true
  and metadata lacks a tenant;
- budget policy rejects the request when its tenant is required but missing;
- singleflight uses the response-cache key, so enabled singleflight inherits
  the cache's tenant partition;
- sensitive chat requests remain non-cacheable unless sensitive caching is
  explicitly enabled, independent of tenant scope.

## Tenant lifecycle

HarborRAG has tenant-aware records and operations but no single tenant lifecycle
service. Onboarding, renaming, retention, and deletion are coordinated operator
workflows.

### Onboard a tenant

A practical onboarding sequence is:

1. Allocate one canonical ID that is valid across every selected backend.
2. Record its display name, environment, owners, and retention policy outside
   HarborRAG.
3. Grant only the required principals and roles in the token issuer.
4. Configure connector, parser, model, and backend settings for the process.
   These catalogs are not selected automatically per tenant.
5. Add optional tenant-specific MCP policy to `config/mcp.yaml`.
6. Submit an explicit tenant-scoped ingestion and retain its task/run ID.
7. Verify the tenant's projection inventory and perform a tenant-scoped
   retrieval before allowing production traffic.
8. Run a negative retrieval and authorization test against at least one other
   tenant.

There is no mandatory `tenant create` command. Some resources, including
tenant-specific Qdrant collections, are created lazily by normal ingestion.
Control-plane projects, sources, members, providers, and workspace settings are
separate records and may need to be seeded by the embedding application or
operator workflow.

### Rename a tenant

A tenant ID is a storage namespace, not a mutable display label. HarborRAG does
not provide an atomic rename across SQL, Qdrant, FalkorDB, object storage,
workflow state, chat memory, caches, token grants, and MCP configuration.

If the display name changes, keep the stable tenant ID and update only the
external display name. If the ID itself must change:

1. stop or drain writes for the old ID;
2. back up every authoritative store and record projection inventory;
3. grant a restricted migration principal temporary access to both IDs;
4. create the new control-plane records and policy entries;
5. reingest from authoritative sources or run a purpose-built, reviewed
   migration that rewrites every tenant-owned record and namespace;
6. compare document, chunk, object, vector, graph, state, and chat counts;
7. run positive and negative retrieval tests for both IDs;
8. switch clients and token grants to the new ID;
9. retain the old namespace for the required rollback window;
10. retire it only through reviewed, tenant-scoped deletion operations.

Changing the fallback environment variable or Qdrant prefix is not a migration.

### Delete or retire a tenant

There is no one-call cascade across all persistence families. A retirement plan
must account for:

- control-plane projects, sources, providers, members, activity, jobs, settings,
  ingestion tasks, and conversation/agent memory;
- canonical SQL documents, versions, chunks, events, and outbox data;
- each tenant-specific Qdrant collection;
- the tenant's nodes and relationships in the shared FalkorDB graph;
- filesystem or S3 objects below the derived tenant prefix;
- SQL or Redis workflow state, checkpoints, leases, and fencing counters;
- model-cache, budget, rate-limit, and singleflight state where configured;
- Temporal workflow histories and external backups subject to their own
  retention policies;
- identity-provider grants, API tokens, and MCP tenant overrides.

Revoke access first, stop new work, back up according to policy, and verify the
exact target before any deletion. Projection deletion does not necessarily
delete authoritative SQL or object-store data, and deleting authoritative data
does not guarantee that every rebuildable projection has disappeared.

## Production readiness checklist

Before treating one deployment as multi-tenant, verify all of the following.

### Identity and authorization

- `HARBORRAG_ENV=prod` and an implemented authenticated mode are enabled.
- Token issuance is controlled by a trusted identity service; tenants cannot
  add themselves to the `tenants` claim.
- Roles follow least privilege, and wildcard tenant grants are exceptional.
- The application authorizes tenant scope before constructing SDK or storage
  contexts.
- HMAC secrets, connector credentials, model keys, database credentials, and
  object-store credentials are loaded from protected deployment configuration.

### Configuration and runtime

- Every automated CLI/API/SDK/MCP call supplies an explicit tenant.
- API replicas and workers use compatible connector, parser, model, storage,
  and Qdrant-prefix settings.
- Durable workflow inputs preserve tenant identity across retry, resume, and
  worker restart.
- Tenant-specific MCP settings are reviewed as policy, not mistaken for data
  provisioning.
- No component assumes that `EngineConfig.tenant` or the process fallback can
  override an explicit request context.

### Storage and network controls

- PostgreSQL, Qdrant, FalkorDB, Redis, object storage, and Temporal are not
  exposed directly to untrusted tenant clients.
- Service credentials have the minimum infrastructure permissions required.
- Backups and restore procedures preserve tenant identity and can restore a
  single tenant when policy requires it.
- Operators know which stores are authoritative and which are rebuildable
  projections before cleanup or disaster recovery.
- Direct database, Cypher, Qdrant, and object-store administration includes an
  explicit tenant target and independent review.

### Observability and incident response

- Tenant IDs are stable, non-secret, and free of personal data because they may
  appear in logs, metrics, traces, collection names, or audit metadata.
- `request_id`, `trace_id`, workflow/task ID, principal, and tenant are recorded
  as separate correlation dimensions.
- Alerts can identify the affected tenant without logging document or model
  content.
- Incident procedures can revoke a tenant grant without rotating unrelated
  tenants and can stop a tenant's new ingestion before cleanup.

## Isolation verification runbook

Do not validate multi-tenancy only by checking that tenant A can read its own
data. Test both positive and negative behavior:

| Test | Tenant A expectation | Tenant B expectation |
| --- | --- | --- |
| Ingest sentinel document A | Run succeeds and records tenant A | No ownership or projection records appear |
| Retrieve a unique phrase from A | Phrase is returned | Phrase is absent |
| Ingest sentinel document B | No changes to A's counts | Run succeeds and records tenant B |
| Retrieve a unique phrase from B | Phrase is absent | Phrase is returned |
| Use A-only API token with tenant B request | `403` | Not applicable |
| Read B task ID with A-only token | `404` to hide task existence | Task is visible to an authorized B reader |
| Invoke MCP tool for B with A-only owner token | Authorization fails | Authorized B owner can invoke it |
| Inspect Qdrant | A and B have distinct physical collections | Neither collection contains the other's points |
| Inspect FalkorDB | All returned nodes/relations have tenant A | All returned nodes/relations have tenant B |
| List object artifacts through HarborRAG | Only A's logical keys are visible | Only B's logical keys are visible |

Repeat the runbook after schema migrations, projection rebuilds, tenant-policy
changes, authentication changes, and restore exercises. Use unique synthetic
sentinel text rather than customer content so a failed test is safe to inspect.

## Security and operational guidance

Multi-tenancy depends on three independent controls:

| Control | Question answered | HarborRAG mechanism |
| --- | --- | --- |
| Authentication | Who is calling? | API/MCP token verification or a trusted embedding boundary. |
| Authorization | May this principal use this tenant and operation? | Tenant grants plus API role or MCP owner requirement. |
| Data isolation | Which records and physical resources can the operation reach? | `AccessContext`, `StorageOperationContext`, tenant filters, namespaced keys, and tenant-specific collections. |

Passing one control does not replace the others. A valid token with no tenant
grant must be denied. A permitted tenant claim without a sufficient role must be
denied. A correctly authorized request must still use tenant-scoped repository
operations.

- Authenticate first, authorize the requested tenant, and only then construct
  `AccessContext`.
- Keep tenant scope in a dedicated top-level field. Do not accept it from
  arbitrary metadata or retrieval filters.
- Use the same tenant ID for ingestion and retrieval. Data ingested into one
  tenant is intentionally invisible from another.
- Restrict wildcard grants and trusted system contexts to internal operations.
- Test isolation in both directions: tenant A must not retrieve tenant B's data,
  and tenant B must not retrieve tenant A's data.
- Assume all principals in one tenant can see the same active retrieval
  evidence. HarborRAG does not yet enforce document-level ACLs inside a tenant.
- Never concatenate an unvalidated tenant into SQL, Cypher, a filesystem path,
  or a provider query. Use the typed contracts and repository adapters.
- Keep source permissions in mind during ingestion: connector credentials may
  read more source data than one tenant should receive, and current retrieval
  does not enforce source-system ACLs per document.
- Treat direct backend access as privileged administration because it can bypass
  application tenant checks.
- Keep model request metadata free of credentials and sensitive content;
  `tenant_id` is correlation and partitioning metadata, not an authorization
  token.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| Retrieval returns no results after a successful ingestion | Compare the ingestion and retrieval tenant IDs exactly, including case. |
| A command reads data under `DEFAULT` | Pass `--tenant` explicitly instead of relying on a command default or runtime fallback. |
| API returns `401` | Check that the bearer token is present, signed with the configured HMAC secret, unexpired, within the maximum lifetime, and uses the configured issuer and audience. |
| API returns `403` | Confirm the role is sufficient and the token's `tenants` claim contains the exact requested tenant or a deliberate `*`. |
| API returns `404` for a known ingestion task | The route intentionally hides tasks outside the principal's tenant scope; verify the task's stored tenant and the token grant. |
| API returns `422` for `tenant` | Use 1–128 characters matching `^[A-Za-z0-9][A-Za-z0-9._-]*$`; use 64 or fewer for widest backend compatibility. |
| API refuses to start with `auth_mode=none` | Production forbids disabled auth; development non-loopback binding requires explicit insecure-development acknowledgement. |
| API refuses to start with `auth_mode=oidc` | OIDC/JWKS verification is not implemented in the current release; use supported HMAC mode or an approved external boundary. |
| MCP returns an owner-role error | MCP authenticated transports currently require an owner token even when the same principal could use reader API routes. |
| MCP denies the requested tenant | Confirm the owner token has the exact tenant grant; tenant-scoped owners cannot access global `*` configuration. |
| MCP configuration rejects a tenant default | `tenant_id` and other required arguments cannot be configured as defaults; callers must supply them. |
| MCP policy changes do not change stored data | Tenant MCP entries affect tool contracts only; ingest, migrate, or delete tenant data separately. |
| Qdrant reports an invalid collection name | Check tenant, logical index, and collection prefix for unsupported characters or excessive length. |
| Qdrant retrieval is empty after changing the collection prefix | The new prefix selects different physical collection names; restore the prefix or rebuild/migrate projections. |
| Graph results appear incomplete | Verify the same tenant was used for vector retrieval and graph traversal and that the active projection was published for that tenant. |
| Object keys are not visible in the raw bucket under the logical name | HarborRAG stores them below an SHA-256-derived tenant prefix; access through the object-store adapter. |
| SDK data crosses a tenant boundary in an embedding application | Audit authentication and authorization before `AccessContext` construction; the SDK trusts the context supplied by the host. |
| A model cache is bypassed or a budget request is rejected | Populate `ModelRequestMetadata.tenant_id` when the configured policy requires tenant isolation. |
| Chat session lookup returns `404` | Confirm tenant, authenticated principal, and session ID all match the session owner. |
| Changing `HARBORRAG_INGESTION_TENANT_ID` does not expose old data | The setting changes future fallback scope; it does not migrate existing tenant data. |
| A renamed tenant has partial data | A rename must migrate every persistence family; compare inventories and return to the old ID until the migration is complete. |
| A `.harbor` directory or workspace name has no effect | Workspace discovery and named runtime profiles are not implemented; select config paths and environment files explicitly. |

## Current workspace limitations

HarborRAG's control plane has tenant-owned projects, members, providers, and a
workspace-settings document. This is not yet a complete workspace lifecycle.
The default runtime does not auto-discover project files, provision tenants,
switch credentials or storage backends by workspace name, or compose a separate
application stack for each tenant. Operators must provide those deployment and
identity-management boundaries explicitly.

In particular, the current workspace concept does not provide:

- a `.harbor`, `.harborrag`, or similar directory that is searched from the
  current working directory;
- named profiles such as `harborrag workspace use engineering`;
- automatic merging of user, repository, environment, and tenant config files;
- per-tenant process environment variables or secret-manager sessions;
- automatic connector/parser/model catalog selection from `tenant_id`;
- automatic databases, buckets, FalkorDB graphs, Redis databases, or Temporal
  namespaces per tenant;
- a complete tenant registry, invitation flow, suspension state, retention
  workflow, atomic rename, or cascading deletion;
- source-system document ACL enforcement within one tenant;
- implemented OIDC/JWKS verification in the API.

You can build stronger isolation around HarborRAG by running separate process
stacks, credentials, databases, buckets, graphs, and Temporal namespaces per
tenant or tenant group. That is deployment composition performed by the host
platform; the tenant ID alone does not create it.

## Related documentation

- [Engine and ingestion runtime configuration](config-file-reference.md)
  covers runtime paths and environment variables.
- [Connector configuration](connector-config.md) explains the process-wide
  connector catalog used by tenant-scoped ingestion requests.
- [Model configuration](model-config.md) covers cache, budget, routing,
  metadata, and credential policy.
- [CLI reference](../cli-reference/README.md) lists exact tenant options and
  command output contracts.
- [Chat](../chat/README.md) documents tenant/principal/session memory identity.
- [MCP Setup and Integration](../detailed-guides/mcp-server/setup-and-integration.md)
  covers authenticated transports, tenant-effective policy, and the local UI.
- [Projection and rebuild architecture](../../developers/architecture/projection-rebuild.md)
  explains authoritative data, tenant projections, and rebuild behavior.
- [Deployment](../../developers/deployment/README.md) covers service topology,
  environment files, and production infrastructure requirements.
