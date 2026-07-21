# Tenant and Workspace Status

HarborRAG does not currently auto-discover per-project configuration, create a `.harbor` workspace, or switch complete runtime stacks by workspace name.

Tenant-aware storage contracts are implemented and are separate from that future workspace concept.

## Tenant identifiers

`harborrag_core.schemas.ids.TenantId` is a non-empty, JSON-compatible typed string used by storage records. The older `harborrag_core.domain.tenant.Tenant` value object also validates a non-empty, whitespace-free ID, but repository APIs use `TenantId` through operation context.

## `StorageOperationContext`

Every repository data operation receives a context:

```python
from harborrag_core.schemas.ids import TenantId
from harborrag_core.schemas.storage import StorageOperationContext

context = StorageOperationContext(
    tenant_id=TenantId("tenant-7"),
    request_id="request-123",
    trace_id="trace-123",
)
```

The context can also carry workflow, ingestion job, retrieval request, document, chunk, actor, and safe metadata identifiers. Repository implementations use it to namespace or filter data and must reject cross-tenant leakage.

## Model tenancy

Model requests accept correlation metadata such as `tenant_id`, `request_id`, and `trace_id`. Tenant IDs are required when configured cache, singleflight, or budget policies demand isolation. This model context is not automatically derived from `EngineConfig.tenant` today.

## Current gaps

A complete multi-tenant runtime still needs one identity/context boundary threaded across connectors, engine stages, model calls, repositories, app routes, and MCP tools. Authentication, authorization, tenant provisioning, workspace discovery, and per-tenant application composition are not supplied by the default local runtime.
