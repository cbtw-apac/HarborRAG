# Ingestion modes

The public ingestion API supports two admission modes: `incremental` and
`force`. The mode controls how discovered source records are evaluated. It does
not replace the connector configuration in `config/connectors.yaml`.

| Mode | Behavior | Recommended use |
| --- | --- | --- |
| `incremental` | Discovers the configured source scope and skips records whose source, processing profile, canonical content, and retrieval metadata are unchanged. | Normal scheduled and manual ingestion. This is the default. |
| `force` | Discovers the same configured scope, then fetches, parses, and evaluates records even when their source descriptors appear unchanged. | Recovering from unreliable source change metadata, validating a connector/parser fix, or deliberately checking every discovered record again. |

## Incremental mode

Submit an incremental ingestion with:

```bash
curl --fail-with-body \
  --request POST \
  --header 'Content-Type: application/json' \
  --header 'Idempotency-Key: confluence-main-2026-08-02' \
  --data '{
    "connection_id": "confluence-main",
    "tenant": "DEFAULT",
    "mode": "incremental"
  }' \
  http://127.0.0.1:8000/v1/ingestions
```

Because `incremental` is the default, omitting `mode` has the same effect.

Incremental mode is change-aware, but it is not automatically a timestamp-only
delta query. HarborRAG scans the source membership defined by the configured
connector and query, records an authoritative source scan, and compares each
record with the active document version. The engine then applies these rules:

- New source record: process and publish it.
- Changed source or canonical content: create and publish a new immutable version.
- Changed retrieval metadata: update the version and rebuild the required projections.
- Changed processing fingerprint: reprocess it even in incremental mode.
- Unchanged record: keep the active version and skip expensive downstream work.
- Missing record: apply the configured consecutive-miss policy before retiring it.

This means `incremental` still detects removals and supports connectors whose
change tokens are not sufficiently reliable. Connector-specific discovery
filters and attachment/comment behavior remain in `config/connectors.yaml`.

There are two change checkpoints. Descriptor admission runs before content is
loaded and can skip a source item cheaply. Canonical admission runs after parsing
and normalization and prevents a new version when the resulting content,
retrieval metadata, and processing fingerprint are identical. See the
[data lifecycle](../developers/architecture/data-lifecycle.md) for the complete
artifact, projection, verification, and publication path.

## Force mode

Submit a force ingestion with:

```bash
curl --fail-with-body \
  --request POST \
  --header 'Content-Type: application/json' \
  --header 'Idempotency-Key: confluence-main-force-2026-08-02' \
  --data '{
    "connection_id": "confluence-main",
    "tenant": "DEFAULT",
    "mode": "force"
  }' \
  http://127.0.0.1:8000/v1/ingestions
```

Force mode bypasses the early unchanged check. Every discovered record is
fetched and normalized again, after which HarborRAG computes its deterministic
document-version identity.

Force mode is intentionally idempotent. If the resulting content, retrieval
metadata, and processing fingerprint are identical to the active version, the
existing version remains active and its completed projections are not rewritten.
If any of those inputs changed, the normal version, projection, verification,
and publication pipeline runs.

Force mode does **not**:

- delete documents, Qdrant collections, graph data, or canonical artifacts;
- create a new version when the deterministic version inputs are identical;
- rebuild already-active projections solely because their storage collection changed;
- override paths, scopes, credentials, attachments, comments, or other connector settings;
- retry only the failed documents from an earlier task.

Use the task's `retry-failures` endpoint to resume retryable failures from
durable artifacts. Use the connector-free reindex workflow when canonical
documents are unchanged but vector or graph projections must be rebuilt, such
as after changing Qdrant collection names. See
[Projection rebuild](../developers/architecture/projection-rebuild.md).

## Choosing a mode

| Situation | Use |
| --- | --- |
| Routine synchronization | `incremental` |
| A connector reports reliable changes and most documents are unchanged | `incremental` |
| Processing configuration changed | `incremental`; the processing fingerprint triggers required reprocessing |
| Source change metadata may be stale or incorrect | `force` |
| Verify a parser or connector fix against every source document | `force` |
| Retry failures from one completed task | `POST /v1/ingestions/{task_id}/retry-failures` |
| Rebuild vector/graph projections from canonical artifacts | Reindex workflow, not `force` |
| Delete obsolete Qdrant collections | Maintenance script after verified reindex, not either ingestion mode |

Use a new `Idempotency-Key` for each ingestion execution you intentionally want
to create. Reusing the same key with the same request returns the existing task.
