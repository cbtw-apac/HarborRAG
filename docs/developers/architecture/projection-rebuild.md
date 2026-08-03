# Projection rebuild after the architecture clean break

The current release remains version `0.1.0`, but its internal vector contract is a
clean break: provider-independent code uses index records and index operations,
while Qdrant collection/point terminology stays inside the Qdrant adapter.

## Inspecting collection drift

Ingestion uses exactly two current Qdrant collections per tenant: one route
collection and one evidence collection. Their physical names are directly
readable, for example `DEFAULT_routes` and `DEFAULT_evidence`. Tenant IDs must
use ASCII letters, digits, `.`, `_`, or `-`; no payload filter is required for
tenant isolation because each tenant still owns separate collections.

Inventory collections before rebuilding or removing any projection:

```bash
python scripts/maintenance/qdrant_collections.py
```

The command labels current, retired single-collection, smoke, and unknown
names. Cleanup is always a dry run unless `--apply` is present:

```bash
python scripts/maintenance/qdrant_collections.py --delete-smoke
python scripts/maintenance/qdrant_collections.py --delete-smoke --apply
```

Legacy hashed route/evidence and `harborrag_chunks` collections may still
contain real projections. Use
`--delete-legacy` only after canonical artifacts have been reindexed and the
new route/evidence projections have been verified. Current-schema collections
can only be deleted by exact name together with `--allow-current`.

PostgreSQL document/version rows and immutable canonical artifacts remain
authoritative. Do not delete them. Qdrant and FalkorDB are rebuildable projections.

For an existing development deployment:

1. Stop API and workers with `scripts/deployment/dev.sh down`.
2. Back up PostgreSQL and the configured object store.
3. Deploy the updated `0.1.0` packages and start data, Temporal, and workers.
4. Submit the existing connector-free reindex workflow for active documents.
5. Verify every new projection manifest before publication.
6. Drain version-addressed cleanup jobs only after the new versions are active.

Cleanup is asynchronous and retryable; it is not a distributed rollback. A failed
projection write must never change the active PostgreSQL document version.
