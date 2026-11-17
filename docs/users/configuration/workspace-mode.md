# Tenant and Workspace Status

Keep each team's or customer's data apart by giving it its own tenant, and use the same
tenant at every entry point: the CLI, the Python SDK, the HTTP API and the MCP tools.
This page explains what a tenant isolates, where each entry point takes its tenant, how
to set up and check a second tenant, and what a workspace is and is not today.

Use a tenant ID to select an isolation boundary. Do not treat it as a workspace profile,
a deployment selector, or proof that a caller is authorized.

## Tenant, principal, project, connection and workspace

These terms are related but not interchangeable:

- A **tenant** is the top-level data and policy boundary. Everything HarborRAG stores,
  indexes and caches belongs to one tenant, identified by its `tenant_id`, and data
  ingested into one tenant is invisible from every other. A new install has one tenant,
  `DEFAULT`.
- A **principal** is the user, service or trusted runtime performing an operation.
  `AccessContext` binds one `principal_id` to one `tenant_id`.
- A **project** is a named group of connections and their documents inside a tenant,
  such as `default`. It does not replace the tenant boundary.
- A **connection** is one configured data source in a project, such as a Jira project or
  a local folder. Its `source_key` is the name the ingestion commands, the SDK and
  schedules use for it. See [Connections](../connections.md#terms-used-on-this-page).
- A **workspace** is control-plane wording for a tenant's settings, members, providers
  and projects. Those records do not create a local configuration profile or switch
  processes, credentials or backends.

## What is implemented

| Capability | Status | Meaning |
| --- | --- | --- |
| Explicit tenant scope in the CLI, SDK, HTTP API and MCP tools | Implemented | Pass the same tenant ID at every entry point that takes part in one workload. |
| Principal and tenant operation context | Implemented | `AccessContext` carries the caller and the tenant through runtime and repository operations. |
| API roles and tenant grants | Implemented | Authenticated requests are checked against the tenants granted to the principal. Development mode can run without authentication. |
| Tenant-specific MCP tool policy | Implemented | `config/mcp/server.yaml` can override tool enablement, defaults and limits per tenant. |
| Tenant-isolated vector and graph retrieval | Implemented | Each tenant has its own Qdrant collections and its own FalkorDB graph. |
| Tenant-isolated object and workflow-state storage | Implemented | Object keys, workflow state, checkpoints and leases include the tenant. |
| Tenant-owned control-plane records | Implemented | Projects, connections, members, activity, jobs and settings carry the tenant. |
| Tenant-owned connections | Implemented | Each connection belongs to one tenant and one project in the control database; ingestion finds it only in that tenant. |
| Read access inside a tenant | Implemented | By default, serving needs current permission snapshots for both the source and the document; one configured tenant can instead share its whole corpus. See [Read access inside a tenant](#read-access-inside-a-tenant). |
| CLI checkout discovery | Implemented | The CLI walks up from the current directory to the repository checkout and runs from there. That selects configuration files, not a tenant by name. |
| OIDC/JWKS authentication | Not implemented | `oidc` is a reserved API mode; the API fails closed if it is selected. |
| Workspace profiles, such as a searched `.harbor` directory | Not implemented | Apart from the CLI's search for its checkout, configuration files and environment variables are selected explicitly. |
| Automatic per-tenant parser, model or process credential selection | Not implemented | Parser and model catalogs and process credentials are shared unless the embedding application composes separate runtimes. Connections, with their stored credentials, are per tenant. |
| Automatic tenant provisioning, or one runtime stack per tenant | Not implemented | Deployment and tenant lifecycle remain operator work. |
| Atomic tenant rename or deletion | Not implemented | Migration and retirement must cover every persistence family explicitly. |

## Choose a tenant ID

`harborrag_core.schemas.ids.TenantId` is the identifier HarborRAG's storage contracts
share. HarborRAG's Pydantic contracts reject empty values and strip leading and trailing
whitespace. Control-plane records reject whitespace, and the runtime environment
validator is stricter still. Public API and Qdrant paths accept up to 128 characters,
while the SQL workflow-state schemas use 64-character tenant columns. For compatibility
with every built-in backend, use a portable identifier that:

- is 1 to 64 characters long (128 is accepted only where a surface documents it);
- starts with an ASCII letter or digit;
- contains only letters, digits, `.`, `_` or `-`;
- contains no secret or personal data.

The portable form matches this regular expression:

```text
^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$
```

For example, use `engineering`, `tenant-7` or `acme.prod`. Tenant comparisons are exact,
so use one canonical spelling and treat IDs as case-sensitive. Renaming a tenant does not
migrate existing records, vector collections, graph nodes, chat sessions or model-policy
state.

`DEFAULT` is convenient for local development. Use explicit, stable IDs when you test
isolation or run a shared environment.

Choose the ID in an identity or deployment control plane, not from a request's free-form
organization name. Keep a registry of the canonical ID, display name, owners, permitted
principals, environment, retention policy and lifecycle state; HarborRAG does not keep
that registry for you.

## Configure the runtime fallback

The runtime reads a fallback tenant for operations that do not receive one explicitly:

```bash
export HARBORRAG_INGESTION_TENANT_ID=tenant-1
```

The default is `DEFAULT`. This variable is a fallback, not a global workspace selector:
CLI commands, API requests, SDK calls and MCP tools can supply their own tenant. Pass the
tenant explicitly whenever isolation matters. The fallback also names the tenant that
`config/sources/connectors.yaml` is imported into, once, on a fresh install.

`EngineConfig.tenant` is only a diagnostics label. It does not build an `AccessContext`,
fill model request metadata, or override the tenant at an application boundary. See
[Engine and ingestion runtime configuration](config-file-reference.md).

### Where each surface takes its tenant

There is no single precedence chain shared by every interface. Each entry point owns its
tenant input:

| Surface | Tenant input | Default | Authorization |
| --- | --- | --- | --- |
| CLI `ingest run`, `ingest start`, `source` and `config` | `--tenant` | `HARBORRAG_INGESTION_TENANT_ID`, else `DEFAULT` | The local CLI trusts the operator; it checks no bearer-token grant. |
| CLI `retrieve`, `chat` and `topology` | `--tenant` | `DEFAULT` | The local CLI trusts the operator; it checks no bearer-token grant. |
| Python SDK ingestion and retrieval | `AccessContext.tenant_id` | None; required | The embedding application must authenticate and authorize the principal before it builds the context. |
| `/v1` HTTP API | The credential's tenant grant; the `X-Tenant-Id` header picks one when the credential holds several | The credential's only tenant; `DEFAULT` for a `*` grant without the header | The API checks the principal's role and tenant grants. |
| MCP tools | Top-level `tenant_id` argument | The tenant a single-tenant key or token is bound to | Authenticated transports check the token's tenant grant; a call naming another tenant is refused. |
| Runtime fallback | `HARBORRAG_INGESTION_TENANT_ID` or `runtime.ingestion_tenant_id` | `DEFAULT` | Used only where a runtime path does not already receive a tenant. |
| Engine diagnostics | `EngineConfig.tenant` | `default` | A diagnostic label only; it does not change operational scope. |

An explicit request value wins, because it travels in that request's `AccessContext` or
public request schema. Changing an environment fallback does not rewrite a CLI argument,
API body, SDK context, durable workflow input or stored data.

### How the SDK and the CLI find configuration

The SDK loads a configuration file only when your application passes its path:

```yaml
execution_mode: temporal
discover_plugins: false
runtime:
  ingestion_tenant_id: tenant-1
  connector_config_path: config/sources/connectors.yaml
  parser_config_path: config/ingestion/parsers.yaml
  model_config_path: config/models/models.yaml
  temporal_target: localhost:7233
```

```python
from harborrag_runtime.sdk import HarborRAG

harbor = HarborRAG.from_config("sdk.yaml")
```

`HarborRAG.from_config` loads exactly the file it is given; the SDK does not search for
one. Keep secrets in the process environment or a secret manager rather than in this
file.

The CLI runs from a repository checkout: it walks up from the current directory to the
checkout and runs from there, with the checkout's `config/` files and the `env/` files
listed in [Run from a repository checkout](../cli-reference/README.md#run-from-a-repository-checkout).
A directory found by walking up is accepted only when you own it and it is not
world-writable. Set the CLI's ingestion fallback tenant with
`HARBORRAG_INGESTION_TENANT_ID`; `retrieve` and `chat` default to `DEFAULT` whatever it
says.

Neither the SDK nor the CLI merges a profile or activates a tenant because a file or
directory name matches it, and there is no `harborrag workspace use` command.

## Use one tenant across entry points

### Use one tenant in the CLI

Pass `--tenant` to each command of a workflow, and make sure the connection exists in
that tenant. Connections are stored per tenant and project in the control database.
`config/sources/connectors.yaml` is imported once, into the tenant that
`HARBORRAG_INGESTION_TENANT_ID` names (`DEFAULT` unless set), under the project
`default`. An imported connection such as `harborrag-workspace` therefore exists only in
that tenant, and `harborrag ingest start --tenant tenant-1 --connector harborrag-workspace`
fails with `ConnectionNotFoundError`.

To ingest for another tenant, give that tenant its own project and connection. From a
repository checkout, `source add` and `source sync` refuse the local SQLite control
database until `HARBORRAG_CONTROL_DB_URL` points at the stack's Postgres; see
[Run from a repository checkout](../cli-reference/README.md#run-from-a-repository-checkout).


1. Create a project in the tenant. The CLI has no project command, so an `admin` sends
    `PUT /v1/admin/projects/{project_id}` with the header `X-Tenant-Id: tenant-1`:

    ```bash
    curl -X PUT -H "Authorization: Bearer $TOKEN" -H "X-Tenant-Id: tenant-1" \
      -H "Content-Type: application/json" \
      https://harborrag.example.com/v1/admin/projects/demo \
      -d '{"project_name": "Demo"}'
    ```

    `201` means the project was created. See [Connections](../connections.md#before-you-start).

2. Write a body file for the connection, such as `handbook.yaml`, in the shape described
    under [Manage connections](../cli-reference/README.md#manage-connections).

3. Create the connection in the tenant, then start its ingestion:

    ```bash
    harborrag source add demo/handbook --file handbook.yaml --tenant tenant-1
    harborrag source sync demo/handbook --tenant tenant-1
    ```

    `source sync` prints the run ID and the command that follows the run,
    `Follow it: harborrag ingest watch <ID>`.

4. Wait for the run, then retrieve and chat in the same tenant:

    ```bash
    harborrag ingest wait <ID>
    harborrag retrieve "deployment requirements" --tenant tenant-1 --top-k 5 --json
    harborrag chat "Summarize the deployment requirements." --tenant tenant-1 --json
    ```

The API can create the connection too, with
`PUT /v1/admin/projects/{project_id}/connections/{connection_id}`. A successful ingestion
can still return no results, because retrieval also needs read access inside the tenant;
see [Read access inside a tenant](#read-access-inside-a-tenant).

Retrieval filters cannot contain `tenant_id`. The top-level `--tenant` option is the only
tenant scope of the command, so a metadata filter cannot override the isolation
boundary. See the [CLI reference](../cli-reference/README.md).

A connection belongs to one tenant and one project, so the same connection name in two
tenants means two separate connections, each with its own source state and projections.
`ingest start` and `ingest run` name a connection by its `source_key`; for a connection
created with `source add` or the API, that is `PROJECT/CONNECTION`, such as
`demo/handbook`. `source sync` always takes `PROJECT/CONNECTION`. Ingestion control
commands such as `status`, `wait`, `pause` and `cancel` use the run ID that
`ingest start` or `source sync` prints; keep that ID with the tenant and connection in
operator records.

### Use one tenant in the Python SDK and repositories

Create the `AccessContext` at the authenticated application boundary and reuse it. When
a repository operation needs durable storage metadata, wrap the access context in a
`StorageOperationContext`:

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

SDK ingestion and retrieval requests require the access context, and reusing one object
makes spelling drift less likely. `connector_name` is the connection's `source_key`,
looked up in the context's tenant, as with the CLI:

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
                connector_name="demo/handbook",
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

`AccessContext` enforces scope; it does not authenticate. The SDK cannot tell whether
`principal_id="user-1"` came from a verified session or from untrusted input. An
embedding service must authenticate the caller, authorize that caller for `tenant-1`,
and only then build the context. The [Python SDK](../python-sdk/README.md) page covers
the SDK itself.

`StorageOperationContext.tenant_id` exposes the enforced tenant to repository
implementations. The context can also carry an operation kind, an idempotency key, a
document ID and a document-version ID.

Trusted background work can use `StorageOperationContext.system("tenant-1", ...)`, which
assigns the fixed runtime principal. Do not use it for an end-user request, because it
replaces the authenticated principal.

### Use one tenant in the HTTP API

The API takes the tenant from the credential. A credential that holds several tenants
names one with `X-Tenant-Id`:

```bash
curl --fail-with-body \
  --header "Authorization: Bearer $TOKEN" \
  --header 'X-Tenant-Id: tenant-1' \
  http://127.0.0.1:8000/v1/admin/connections
```

Without the header, such a credential gets `400 tenant_required`; a credential with one
tenant acts in it, and a `*` credential acts in `DEFAULT`. A tenant the credential does
not hold gets `403 tenant_forbidden`, whatever its role. Chat requests take the tenant
the same way; the request body carries no tenant.

`HARBORRAG_AUTH_MODE=none` is a development-only mode that treats every request as an
`owner` with access to every tenant. Production configuration refuses to start with
authentication off. A tenant ID is a routing label, not a credential; never authorize a
request from its claimed tenant alone.

#### Configure API authentication

The authenticated API mode is `hmac`: the API verifies HS256-signed JWT bearer tokens.
In that mode it also accepts an API key created with `harborrag auth keys create` (see
[MCP setup](../detailed-guides/mcp-server/setup-and-integration.md)), as a `reader` of
the key's one tenant. The settings are:

| Variable | Requirement or default | Purpose |
| --- | --- | --- |
| `HARBORRAG_ENV` | `dev`; set `prod` for production | `staging` and `prod` turn on the fail-closed checks. |
| `HARBORRAG_AUTH_MODE` | `none`; use `hmac` for an exposed service | Selects the token verifier. `oidc` is reserved but not implemented. |
| `HARBORRAG_AUTH_SECRET` | Required for `hmac`; at least 32 UTF-8 bytes | Verifies HS256 signatures. Load it from protected deployment configuration. |
| `HARBORRAG_AUTH_USER_ID_CLAIM` | `sub` | Signed claim that owns chat and user-scoped memory. Required in every token. |
| `HARBORRAG_AUTH_ISSUER` | `harborrag` | Required `iss` claim. |
| `HARBORRAG_AUTH_AUDIENCE` | `harborrag-api` | Required `aud` claim. |
| `HARBORRAG_AUTH_MAX_TOKEN_LIFETIME_SECONDS` | `3600` | Maximum accepted difference between `iat` and `exp`. |
| `HARBORRAG_AUTH_CLOCK_SKEW_SECONDS` | `30` | Permitted verification clock skew. |

Do not commit the HMAC secret or use it as a tenant identifier. Production also needs the
other production settings in [Engine and ingestion runtime configuration](config-file-reference.md);
authentication settings alone are not a complete deployment.

A verified token must carry claims equivalent to this payload:

```json
{
  "sub": "service-release-bot",
  "oid": "user-42",
  "role": "editor",
  "tenants": ["tenant-1"],
  "iat": 1787558400,
  "exp": 1787562000,
  "iss": "harborrag",
  "aud": "harborrag-api"
}
```

With `HARBORRAG_AUTH_USER_ID_CLAIM=oid`, this request belongs to `user-42`, while `sub`
stays the credential actor. Issue a stable signed `oid` for each person behind a shared
service credential. If the configured claim is missing or invalid, the request returns
`401`. With the default setting, `sub` is both actor and user identity. The request body
cannot set or override this identity. Sessions created under the former shared
`DEFAULT_USER` identity need an explicit trusted owner mapping before they can move to
individual users.

The `tenants` value must be a non-empty list of non-empty strings. Matching is exact and
case-sensitive. `"*"` grants every tenant; a high role without a matching tenant grant is
still rejected.

The API role ladder is `reader < editor < admin < owner`:

| Role | Tenant-relevant access |
| --- | --- |
| `reader` | Chat and read-only routes within granted tenants. |
| `editor` | Reader access plus connections, ingestion and other editor changes. |
| `admin` | Editor access plus administrative routes, such as creating projects. |
| `owner` | Highest API role. The MCP server's configuration editor and management API also require `owner`; MCP tool calls accept `reader` or `owner`. |

Failures are distinguishable where that is safe:

- missing, expired, malformed or wrongly signed credentials return `401`;
- an insufficient role or a tenant the credential does not hold returns `403`;
- an ingestion run outside the principal's tenants returns `404`, which hides whether
  that run exists;
- an invalid tenant shape in a `/v1` request returns `422` before work starts.

With authentication off, the API serves loopback connections only, unless an operator
sets `HARBORRAG_ALLOW_INSECURE_DEV=true`. That flag acknowledges an unauthenticated
development listener; it is not an authentication mechanism.

### Use one tenant in MCP tools

MCP tools take the tenant in a top-level `tenant_id` argument:

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

Authenticated MCP transports accept a `reader` or `owner` token for tool calls and check
the requested tenant against the token's grant. A key or token bound to one tenant acts
in that tenant, and a call naming another tenant is refused; local stdio without a token
needs an explicit `tenant_id`. As with CLI retrieval, `tenant_id` cannot appear inside
`filters`. Transport authentication and reader keys are in
[MCP Setup and Integration](../detailed-guides/mcp-server/setup-and-integration.md).

Tenant entries in `config/mcp/server.yaml` change the effective tool catalog and policy;
they do not create tenants or move data. A tenant entry merges over the global settings,
environment overrides apply after the file, and request arguments are checked last,
against the resolved schema and limits. A tenant entry can lower limits or disable a
tool, but it cannot raise a compiled safety ceiling or set a default for `tenant_id` or
any other required argument, so a caller always states its tenant. Unknown tenants use
the global policy. The file format and its validation rules are in
[Tenant overrides](../detailed-guides/mcp-server/setup-and-integration.md#tenant-overrides)
and [Validation](../detailed-guides/mcp-server/setup-and-integration.md#validation).

Calling `McpServer.call_tool(...)` directly is a trusted in-process boundary: its
`principal_id` labels audit records but does not authenticate the caller. Applications
that embed the server directly must authorize callers themselves.

## Read access inside a tenant

A tenant boundary alone does not let every principal in the tenant read every document.
Retrieval, chat and the MCP tools apply one of two corpus access modes:

- `source_acl` (the default): a principal reads a document only when there are current,
  trusted permission snapshots that allow it, for both the document's source and the
  document. Missing or expired snapshots deny access, and connectors do not synchronize
  these snapshots yet. Import them with
  `harborrag topology indexing permissions-import`, and check coverage with
  `harborrag topology indexing permissions-status --tenant <tenant>`.
- `tenant_shared`: every principal in one configured tenant reads all of that tenant's
  published documents. Set `HARBORRAG_CORPUS_ACCESS_MODE=tenant_shared` and name the
  tenant in `HARBORRAG_CORPUS_SHARED_TENANT_ID`; every other tenant stays on
  `source_acl`, even in the same process.

The CLI, the API and the MCP server each read these two variables from their own
environment. In the SDK, the application chooses the mode per request with
`AccessContext.corpus_mode`. For the CLI from a repository checkout, see
[Retrieve evidence](../cli-reference/README.md#retrieve-evidence); for MCP, see
[Tenant-bound reader keys and shared corpus access](../detailed-guides/mcp-server/setup-and-integration.md#tenant-bound-reader-keys-and-shared-corpus-access).

## How tenant context propagates

The safest design has one decision point at the application boundary and no later
tenant re-selection:

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

| Field | Contract |
| --- | --- |
| `principal_id` | Required, non-empty caller identity, up to 255 characters. |
| `tenant_id` | Required `TenantId`, used as the operational data boundary. |
| `corpus_mode` | `source_acl` (default) or `tenant_shared`; see [Read access inside a tenant](#read-access-inside-a-tenant). |

Create one after authentication and tenant authorization. Pass the same context through
SDK ingestion, vector retrieval and graph retrieval. Do not rebuild it from document
metadata, a connector payload or model output.

### `StorageOperationContext`

Repository methods receive a `StorageOperationContext`, which keeps the `AccessContext`
and adds durable operation metadata:

| Field | Purpose |
| --- | --- |
| `access` | Authenticated or trusted principal and the enforced tenant. |
| `operation_kind` | Bounded operation label, such as `vector.search`; defaults to `unspecified`. |
| `idempotency_key` | Optional replay and deduplication identity of a durable operation. |
| `document_id` | Optional document correlation identity. |
| `document_version_id` | Optional immutable-version correlation identity. |

`StorageOperationContext.for_access(...)` keeps the end-user principal.
`StorageOperationContext.system(...)` creates the fixed `harborrag-runtime` principal for
trusted background work. Repository records that already contain a tenant must match
`context.tenant_id`; adapters reject a mismatch rather than silently rewriting record
ownership.

### Model request metadata

`ModelRequestMetadata` carries correlation fields such as `request_id`, `trace_id`,
`tenant_id`, `user_id`, `workflow_id`, collection and pipeline stage. It is separate from
`AccessContext` on purpose, because model provider metadata is not an authorization
object.

When you call model adapters directly, copy the authorized tenant explicitly:

```python
from harborrag_core.models import ModelRequestMetadata

model_metadata = ModelRequestMetadata(
    request_id="request-123",
    trace_id="trace-123",
    tenant_id=str(access.tenant_id),
    user_id=access.principal_id,
)
```

Do not derive tenant authorization from model metadata on the return path. Provider
headers and telemetry may carry a sanitized or hashed tenant identifier, but the original
`AccessContext` stays the data-access authority.

## How data isolation works

Tenant isolation is enforced at several layers:

| Layer | Isolation behavior |
| --- | --- |
| Control-plane repositories | Tenant-owned records carry `tenant_id`; repository contracts expose tenant-scope filters. |
| Canonical SQL repositories | Documents, versions, chunks, events and outbox records store the tenant and use it in reads and ownership checks. |
| Qdrant | Each tenant has physically separate collections. `tenant_id` is deliberately not a payload filter. |
| FalkorDB | Each tenant has a physically separate graph. `tenant_id` also takes part in node identity, uniqueness constraints, writes and retrieval filters. |
| Filesystem and S3 object stores | Logical object keys sit below the tenant's namespace prefix, `{storage_namespace_prefix}_{tenant_id}/`. |
| SQL and Redis workflow state | Workflow state, checkpoints, leases and fencing counters include the tenant in keys or ownership checks. |
| Temporal ingestion | The tenant is stored in workflow and source inputs, so retries and resumed activities keep the original scope. |
| Chat memory | Sessions and turns are keyed by tenant, principal and session ID. |
| Model cache, singleflight and budgets | Model request metadata supplies `tenant_id`; configured policies can require it and partition state by tenant. |
| MCP tool policy | The tenant selects effective enablement, defaults and limits after transport authorization; the runtime context still provides data isolation. |

FalkorDB's physical partition and its in-graph tenant predicates are both required; one
does not replace the other. See
[Projection and rebuild architecture](../../developers/architecture/projection-rebuild.md#the-tenant-spine)
for the storage design.

### One tenant namespace in every store

Every store names a tenant the same way, so finding or removing everything a tenant owns
takes one name per store:

```text
{storage_namespace_prefix}_{tenant_id}          # e.g. harborrag_DEFAULT
```

| Store | Physical name |
| --- | --- |
| Qdrant | `harborrag_DEFAULT_{logical_index}` (one collection per logical index, e.g. `_evidence`) |
| FalkorDB | graph `harborrag_DEFAULT` |
| S3 / MinIO, filesystem | key prefix `harborrag_DEFAULT/{logical_key}` in each bucket |
| Postgres | rows with `tenant_id = 'DEFAULT'` |

`HARBORRAG_STORAGE_NAMESPACE_PREFIX` (default `harborrag`) sets the prefix for all of
them at once; it replaces the former `HARBORRAG_QDRANT_COLLECTION_PREFIX` and
`HARBORRAG_FALKORDB_TENANT_GRAPH_PREFIX`. Changing it points the runtime at different
physical names; it does not rename or move existing data.

The tenant must be 1 to 128 ASCII letters, digits, `.`, `_` or `-`, beginning with a
letter or digit, and the prefix 1 to 64 letters, digits, `_` or `-`, beginning with a
letter. The namespace is derived from the trusted operation context before any query or
key is built, never from user input. That is why `tenant_id` is absent from the Qdrant
payload and cannot be used as a user-supplied filter to jump between collections.

Each evidence point also stores `graph_chunk_node_key` and `graph_source_node_key`: the
chunk's node and its document's source entity in the tenant graph, so a vector hit joins
the graph without re-deriving a node key.

The graph adapter includes `tenant_id` in merge identities, uniqueness constraints,
traversal predicates, administrative counts, cleanup and projection deletion. This
matters even when a node key is deterministic: two tenants can ingest identical content
and produce the same document-version or chunk key.

Graph queries must keep using repository methods that receive a
`StorageOperationContext`. Do not run application-supplied Cypher directly against a
graph, because that bypasses the tenant predicates.

### Object-store isolation

Filesystem and S3 adapters map a logical object key into the tenant namespace:

```text
{storage_namespace_prefix}_{tenant_id}/{logical_key}
```

Listing, reading, writing, presigning and deletion compute the prefix from
`context.tenant_id`. This is namespacing, not encryption: bucket policy and
infrastructure credentials must still prevent direct access that bypasses HarborRAG.

### Relational and workflow-state isolation

Canonical and control-plane records store `tenant_id` in indexed columns, composite
identities or explicit scope filters. Workflow-state adapters use the tenant in state,
checkpoint, lease and fencing identities. Records passed to state repositories are
checked against the operation context, so a record for tenant A cannot be written with
tenant B's context.

The process normally shares one set of database credentials. Row-level tenant columns
are therefore an application-enforced boundary, not a separate database account or
schema per tenant. Operators who need physical database isolation must compose separate
deployments or adapters.

### Model-policy isolation

When you use model adapters directly, put the same tenant into
`ModelRequestMetadata.tenant_id`. A tenant-isolated cache or budget policy may bypass or
reject a request without it. See [Model configuration](model-config.md). The exact
behavior depends on the policy:

- response caching bypasses the cache when `require_tenant_id` is true and the metadata
  has no tenant;
- budget policy rejects the request when its tenant is required but missing;
- singleflight uses the response-cache key, so enabled singleflight inherits the cache's
  tenant partition;
- sensitive chat requests stay uncacheable unless sensitive caching is explicitly
  enabled, whatever the tenant scope.

## Tenant lifecycle

HarborRAG has tenant-aware records and operations but no tenant lifecycle service.
Onboarding, renaming, retention and deletion are operator workflows.

### Onboard a tenant

A practical onboarding sequence:

1. Allocate one canonical ID that is valid in every selected backend.
2. Record its display name, environment, owners and retention policy outside HarborRAG.
3. Grant only the required principals and roles in the token issuer.
4. Create the tenant's projects and connections through the API, or its connections with
   `harborrag source add --tenant`; `config/sources/connectors.yaml` is imported into one
   tenant only. Parser, model and backend settings are shared by the process and are not
   selected per tenant.
5. Add tenant-specific MCP policy to `config/mcp/server.yaml`, if needed.
6. Decide the tenant's read access: import permission snapshots, or make it the shared
   tenant (see [Read access inside a tenant](#read-access-inside-a-tenant)).
7. Start a tenant-scoped ingestion and keep its run ID.
8. Check the tenant's projection inventory and run a tenant-scoped retrieval before you
   allow production traffic.
9. Run a negative retrieval and authorization test against at least one other tenant.

There is no `tenant create` command. Some resources, such as the tenant's Qdrant
collections, are created on demand by normal ingestion. Control-plane projects,
connections, members, providers and workspace settings are separate records that the
embedding application or the operator may need to create.

### Rename a tenant

A tenant ID is a storage namespace, not a display label. HarborRAG has no atomic rename
across SQL, Qdrant, FalkorDB, object storage, workflow state, chat memory, caches, token
grants and MCP configuration.

When only the display name changes, keep the tenant ID and update the external display
name. If the ID itself must change:

1. stop or drain writes for the old ID;
2. back up every authoritative store and record the projection inventory;
3. grant a restricted migration principal temporary access to both IDs;
4. create the new control-plane records and policy entries;
5. reingest from the authoritative sources, or run a purpose-built, reviewed migration
   that rewrites every tenant-owned record and namespace;
6. compare document, chunk, object, vector, graph, state and chat counts;
7. run positive and negative retrieval tests for both IDs;
8. switch clients and token grants to the new ID;
9. keep the old namespace for the required rollback window;
10. retire it only through reviewed, tenant-scoped deletion.

Changing the fallback environment variable or the storage namespace prefix is not a
migration.

### Delete or retire a tenant

There is no one-call cascade across all persistence families. A retirement plan must
cover:

- control-plane projects, connections, providers, members, activity, jobs, settings,
  ingestion runs and conversation memory;
- canonical SQL documents, versions, chunks, events and outbox data;
- each of the tenant's Qdrant collections;
- the tenant's FalkorDB graph;
- filesystem or S3 objects below the tenant's namespace prefix;
- SQL or Redis workflow state, checkpoints, leases and fencing counters;
- model-cache, budget, rate-limit and singleflight state, where configured;
- Temporal workflow histories and external backups, under their own retention policies;
- identity-provider grants, API tokens, MCP keys and MCP tenant overrides.

Revoke access first, stop new work, back up according to policy, and confirm the exact
target before any deletion. Deleting projections does not necessarily delete
authoritative SQL or object-store data, and deleting authoritative data does not
guarantee that every rebuildable projection is gone.

## Check production readiness

Before you treat one deployment as multi-tenant, confirm all of the following.

### Identity and authorization

- `HARBORRAG_ENV=prod` and an implemented authenticated mode are set.
- A trusted identity service issues tokens; tenants cannot add themselves to the
  `tenants` claim.
- Roles follow least privilege, and wildcard tenant grants are exceptional.
- The application authorizes the tenant before it builds SDK or storage contexts.
- HMAC secrets, connector credentials, model keys, database credentials and object-store
  credentials come from protected deployment configuration.

### Configuration and runtime

- Every automated CLI, API, SDK and MCP call supplies an explicit tenant.
- API replicas and workers use compatible connector, parser, model, storage and
  namespace-prefix settings.
- Durable workflow inputs keep the tenant across retry, resume and worker restart.
- Tenant-specific MCP settings are reviewed as policy, not mistaken for data
  provisioning.
- No component assumes that `EngineConfig.tenant` or the process fallback can override
  an explicit request context.

### Storage and network controls

- PostgreSQL, Qdrant, FalkorDB, Redis, object storage and Temporal are not exposed to
  untrusted tenant clients.
- Service credentials have the minimum infrastructure permissions they need.
- Backups and restores keep the tenant and can restore a single tenant when policy
  requires it.
- Operators know which stores are authoritative and which are rebuildable projections
  before cleanup or disaster recovery.
- Direct database, Cypher, Qdrant and object-store administration names an explicit
  tenant and is reviewed independently.

### Observability and incident response

- Tenant IDs are stable, non-secret and free of personal data, because they may appear
  in logs, metrics, traces, collection names and audit metadata.
- `request_id`, `trace_id`, workflow or run ID, principal and tenant are recorded as
  separate correlation dimensions.
- Alerts can name the affected tenant without logging document or model content.
- Incident procedures can revoke a tenant grant without rotating unrelated tenants, and
  can stop a tenant's new ingestion before cleanup.

## Verify isolation in both directions

Do not validate multi-tenancy only by checking that tenant A can read its own data. Test
both positive and negative behavior:

| Test | Tenant A expectation | Tenant B expectation |
| --- | --- | --- |
| Ingest sentinel document A | Run succeeds and records tenant A | No ownership or projection records appear |
| Retrieve a unique phrase from A | Phrase is returned | Phrase is absent |
| Ingest sentinel document B | A's counts do not change | Run succeeds and records tenant B |
| Retrieve a unique phrase from B | Phrase is absent | Phrase is returned |
| Use an A-only API token with a tenant B request | `403` | Not applicable |
| Read a B run ID with an A-only token | `404`, hiding that the run exists | The run is visible to an authorized B reader |
| Call an MCP tool for B with an A-only token | Authorization fails | A token granted B can call it |
| Inspect Qdrant | A and B have distinct physical collections | Neither collection holds the other's points |
| Inspect FalkorDB | All returned nodes and relations have tenant A | All returned nodes and relations have tenant B |
| List object artifacts through HarborRAG | Only A's logical keys are visible | Only B's logical keys are visible |

Repeat these tests after schema migrations, projection rebuilds, tenant-policy changes,
authentication changes and restore exercises. Use unique synthetic sentinel text rather
than customer content, so a failed test is safe to inspect.

## Security and operational guidance

Multi-tenancy depends on three independent controls:

| Control | Question answered | HarborRAG mechanism |
| --- | --- | --- |
| Authentication | Who is calling? | API or MCP token verification, or a trusted embedding boundary. |
| Authorization | May this principal use this tenant and operation? | Tenant grants plus the API role or the MCP token role. |
| Data isolation | Which records and physical resources can the operation reach? | `AccessContext`, `StorageOperationContext`, tenant filters, namespaced keys and per-tenant collections and graphs. |

Passing one control does not replace the others. A valid token with no tenant grant is
denied. A permitted tenant without a sufficient role is denied. A correctly authorized
request must still use tenant-scoped repository operations.

- Authenticate first, authorize the requested tenant, and only then build the
  `AccessContext`.
- Keep the tenant in a dedicated top-level field; never accept it from metadata or
  retrieval filters.
- Use the same tenant for ingestion and retrieval.
- Restrict wildcard grants and trusted system contexts to internal operations.
- Resolve source-system groups, inherited denies and other access rules before you
  import effective permission decisions. Connector credentials may read more than the
  serving principal may.
- Never concatenate an unvalidated tenant into SQL, Cypher, a filesystem path or a
  provider query; use the typed contracts and repository adapters.
- Treat direct backend access as privileged administration, because it can bypass the
  application's tenant checks.
- Keep model request metadata free of credentials and sensitive content; `tenant_id` is
  correlation and partitioning metadata, not an authorization token.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| Retrieval returns no results after a successful ingestion | Compare the tenant IDs, then check read access: run `harborrag topology indexing permissions-status --tenant <tenant>`, or confirm the tenant is the configured shared tenant. See [Read access inside a tenant](#read-access-inside-a-tenant). Indexed vectors alone do not grant access. |
| A command reads data under `DEFAULT` | Pass `--tenant` explicitly instead of relying on a command default or the runtime fallback. `HARBORRAG_INGESTION_TENANT_ID` changes the default only for `ingest`, `source` and `config`. |
| `harborrag ingest start --tenant` fails with `ConnectionNotFoundError` | The connection is not in that tenant. Connections imported from `config/sources/connectors.yaml` are in `DEFAULT`, unless `HARBORRAG_INGESTION_TENANT_ID` named another tenant at import; create the connection in this tenant with `harborrag source add --tenant` or the API. |
| The CLI refuses a checkout that you do not own or that is world-writable | A directory found by walking up must be yours and not writable by others. Fix its ownership or permissions. |
| API returns `400 tenant_required` | The credential holds several tenants; send `X-Tenant-Id`. |
| API returns `401` | Check that the bearer token is present, signed with the configured HMAC secret, unexpired, within the maximum lifetime, and has the configured issuer and audience. |
| API returns `403` | Confirm the role is sufficient and the token's `tenants` claim contains the exact requested tenant, or a deliberate `*`. |
| API returns `404` for a known ingestion run | The API hides runs outside the principal's tenants; check the run's tenant and the token grant. |
| API returns `422` for a tenant | Use 1 to 128 characters matching `^[A-Za-z0-9][A-Za-z0-9._-]*$`; use 64 or fewer for the widest backend compatibility. |
| API refuses to start with `auth_mode=none` | Production forbids disabled authentication; a development listener on a non-loopback address needs `HARBORRAG_ALLOW_INSECURE_DEV=true`. |
| API refuses to start with `auth_mode=oidc` | OIDC/JWKS verification is not implemented; use `hmac` or an approved external boundary. |
| MCP returns `owner role required` | A `reader` key can list and run tools but cannot change the server's configuration; use the owner token for the configuration editor and management API. |
| MCP denies the requested tenant | Confirm the key or token is granted that exact tenant; a key bound to one tenant cannot act in another. |
| MCP configuration rejects a tenant default | `tenant_id` and other required arguments cannot be configured as defaults; callers must supply them. |
| MCP policy changes do not change stored data | Tenant MCP entries affect tool contracts only; ingest, migrate or delete tenant data separately. |
| Qdrant reports an invalid collection name | Check the tenant, logical index and namespace prefix for unsupported characters or excessive length. |
| Qdrant retrieval is empty after changing the namespace prefix | The new prefix selects different physical names; restore the prefix or rebuild the projections. |
| Graph results look incomplete | Check that vector retrieval and graph traversal used the same tenant, and that the active projection was published for that tenant. |
| Object keys are not where you expect in the raw bucket | HarborRAG stores them below the tenant's namespace prefix, such as `harborrag_DEFAULT/`; read them through the object-store adapter. |
| SDK data crosses a tenant boundary in an embedding application | Audit authentication and authorization before the `AccessContext` is built; the SDK trusts the context the host supplies. |
| A model cache is bypassed or a budget request is rejected | Fill `ModelRequestMetadata.tenant_id` when the configured policy requires tenant isolation. |
| Chat session lookup returns `404` | Confirm that the tenant, the authenticated principal and the session ID all match the session's owner. |
| Changing `HARBORRAG_INGESTION_TENANT_ID` does not show old data | The setting changes the future fallback only; it does not migrate existing tenant data. |
| A renamed tenant has partial data | A rename must migrate every persistence family; compare inventories, and return to the old ID until the migration is complete. |
| A `.harbor` directory or a workspace name has no effect | Workspace profiles are not implemented. The CLI reads the configuration of the checkout it runs in; the SDK and services read the configuration paths and environment they are given. |

## What a workspace does not provide yet

HarborRAG's control plane has tenant-owned projects, members, providers and a
workspace-settings document. That is not yet a complete workspace lifecycle. In
particular, it does not provide:

- a `.harbor`, `.harborrag` or similar profile directory searched from the current
  directory (the CLI searches only for its repository checkout);
- named profiles, such as `harborrag workspace use engineering`;
- automatic merging of user, repository, environment and tenant configuration files;
- per-tenant process environment variables or secret-manager sessions;
- automatic parser or model catalog selection from `tenant_id`;
- automatic databases, buckets, Redis databases or Temporal namespaces per tenant;
- a tenant registry, invitation flow, suspension state, retention workflow, atomic
  rename or cascading deletion;
- automatic synchronization of source-system document permissions into permission
  snapshots;
- OIDC/JWKS verification in the API.

You can build stronger isolation around HarborRAG by running separate process stacks,
credentials, databases, buckets, graphs and Temporal namespaces per tenant or group of
tenants. That is deployment composition done by the host platform; the tenant ID alone
does not create it.

## Related documentation

- [Engine and ingestion runtime configuration](config-file-reference.md) covers runtime
  paths and environment variables.
- [Connections](../connections.md) covers creating, syncing and changing a tenant's
  connections.
- [Connector configuration](connector-config.md) explains
  `config/sources/connectors.yaml`, which is imported once into one tenant.
- [Model configuration](model-config.md) covers cache, budget, routing, metadata and
  credential policy.
- [CLI reference](../cli-reference/README.md) lists the exact tenant options and output
  contracts.
- [Chat](../chat/README.md) documents tenant, principal and session memory identity.
- [MCP Setup and Integration](../detailed-guides/mcp-server/setup-and-integration.md)
  covers authenticated transports, reader keys, tenant policy and the local UI.
- [Projection and rebuild architecture](../../developers/architecture/projection-rebuild.md)
  explains authoritative data, tenant projections and rebuilds.
- [Deployment](../../developers/deployment/README.md) covers service topology,
  environment files and production infrastructure.
