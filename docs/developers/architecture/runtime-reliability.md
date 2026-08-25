# Runtime Reliability

HarborRAG offers direct ingestion for local and embedded use and Temporal-backed ingestion for
durable distributed execution. Both paths call the same engine policies. Orchestration decides
when work runs; it does not redefine whether a document version is safe to publish.

## Where Temporal participates

Temporal coordinates long-running ingestion, failed-document retry, reindex, and reconciliation.
It provides durable history, retries, signals, queries, and worker coordination around external
side effects.

Retrieval does not use a workflow. Keeping the read path direct avoids workflow scheduling latency
and makes the active-version check explicit at request time.

```text
operator/API -> workflow -> activities -> engine policies -> storage ports
reader/MCP   -> retrieval service --------> engine policies -> storage ports
```

## Workflow and activity boundaries

Workflows contain deterministic coordination: phase order, durable progress, cancellation state,
and the decision to schedule an activity. Activities contain I/O: connector calls, parsing,
artifact writes, model requests, projection writes, and authority updates.

This split matters because workflow histories can be replayed. Workflow code must not depend on
ambient time, random values, mutable process state, or direct provider calls. Changes to deployed
workflow behavior require replay-aware versioning and compatibility review.

## Idempotency and retries

External effects use stable tenant, source, document, version, artifact, and projection identities.
A retry should either observe the existing compatible result or safely write the same logical
result. It must not create a second authoritative version merely because an activity was delivered
again.

Retry policy belongs at the boundary that understands the failure:

- Provider adapters classify transient transport and throttling failures.
- Engine policy decides whether a failed artifact or projection can be resumed or must be rebuilt.
- Runtime chooses orchestration retry and timeout policy for the deployment.
- PostgreSQL publication remains an explicit, atomic operation after verification.

The project intentionally avoids publishing universal worker counts, timeouts, or retry numbers.
Those values depend on document size, provider quotas, model latency, and the resources assigned to
each deployment.

## Safe operator controls

Temporal workflows can expose status queries and accept pause, resume, and graceful cancellation
signals. A pause stops scheduling new expensive work after the current safe boundary. Graceful
cancellation preserves already written immutable artifacts and prevents an unverified version from
becoming active.

Forceful infrastructure termination is different from a domain cancellation. Workers must be able
to restart, replay history, and continue from idempotent effects without treating process memory as
durable state.

## Failure behavior

| Failure | Expected safety outcome |
| --- | --- |
| Connector or parser fails | Version remains inactive; diagnostic state is retained |
| Artifact write is retried | Stable artifact identity prevents duplicate logical evidence |
| One projection fails | Publication does not advance until required projections verify |
| Worker restarts | Workflow history replays and resumes at activity boundaries |
| Old projection cleanup fails | Active version remains valid; cleanup is retried separately |
| Vector store returns a stale version | Retrieval removes it through the authority check |
| Graph store is unavailable | Policy can return vector evidence without claiming graph context |

## Deployment responsibilities

The repository provides local service compositions and executable API, worker, CLI, and MCP
surfaces. A production operator must still define:

- authentication, authorization, TLS, and network trust boundaries;
- secret storage, rotation, and least-privilege provider credentials;
- database and object-store backup, restore, retention, and disaster recovery;
- Temporal namespace, task-queue, worker rollout, and replay compatibility policy;
- resource limits, autoscaling, provider quotas, alerting, and incident response;
- tenant-isolation acceptance tests appropriate to the deployment.

Start with the [deployment guide](../deployment/README.md) and validate the
[data lifecycle](data-lifecycle.md) invariants before exposing a service to untrusted traffic.
