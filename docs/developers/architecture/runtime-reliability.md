# Runtime Reliability

This page explains how HarborRAG keeps ingestion safe when workers restart, providers fail or
an operator pauses a run. It is for contributors who change workflows, activities or retry
behaviour, and for operators who plan a deployment.

HarborRAG has two ways to run ingestion. **Direct ingestion** runs in the calling process, for
local and embedded use, such as `harborrag ingestions start --inline`. **Durable ingestion**
runs on [Temporal](https://docs.temporal.io/), a workflow engine that records each run's
progress so it survives process restarts. Both paths call the same engine policies:
orchestration decides *when* work runs, never *whether* a document version is safe to publish.

## Where Temporal participates

Temporal coordinates long-running source ingestion, scheduled ingestion, retries of failed
documents, and reindex. It provides durable history, retries, signals, queries and worker
coordination around external side effects. The worker registers these workflow types:

| Workflow type | Purpose |
| --- | --- |
| `harborrag.source_ingestion` | One ingestion run of a connection |
| `harborrag.scheduled_source_ingestion` | A run started by a schedule declared in `config/sources/schedules.yaml`; see [Scheduled Ingestion](../../users/scheduled-ingestion.md#declare-schedules-in-a-file) |
| `harborrag.source_batch` | A batch of discovered documents within a run |
| `harborrag.document_ingestion` | One document's release |
| `harborrag.retry_failures` | A retry of recorded failures |
| `harborrag.document_retry` | One failed document's retry |
| `harborrag.reindex` | A projection rebuild from canonical artifacts |

The API registers one more workflow type, `harborrag.schedule_slot`: one slot of a
schedule created with `/v1/admin/schedules`. Its worker runs inside the API process on
the task queue `harborrag-schedules` (`HARBORRAG_SCHEDULE_TASK_QUEUE`), because a slot
starts its run through the code path of `POST /v1/admin/ingestions`, which only the API
composes. The ingestion worker does not register it; see
[How a schedule runs](../../users/scheduled-ingestion.md#how-a-schedule-runs).

Task queues, timeouts and worker settings are described in
[Temporal configuration](../../../deploy/temporal/README.md).

Retrieval never uses a workflow. Keeping the read path direct avoids workflow scheduling
latency and makes the active-version check explicit at request time.

```text
operator/API -> workflow -> activities -> engine policies -> storage ports
reader/MCP   -> retrieval service --------> engine policies -> storage ports
```

## Workflow and activity boundaries

A [workflow](https://docs.temporal.io/workflows) holds deterministic coordination: phase
order, durable progress, cancellation state, and the decision to schedule an activity. An
[activity](https://docs.temporal.io/activities) holds the I/O: connector calls, parsing,
artifact writes, model requests, projection writes and authority updates.

The split matters because Temporal replays workflow history. Workflow code must therefore
follow Temporal's
[deterministic constraints](https://docs.temporal.io/workflow-definition#deterministic-constraints):
no ambient time, random values, mutable process state or direct provider calls. A change to
deployed workflow behaviour needs
[versioning](https://docs.temporal.io/develop/python/versioning) and a replay compatibility
review.

## Idempotency and retries

External effects use stable tenant, source, document, version, artifact and projection
identities. A retried activity either finds the existing compatible result or writes the same
logical result again. It must never create a second authoritative version just because an
activity was delivered twice.

Retry policy belongs at the boundary that understands the failure:

- Provider adapters classify transient transport and throttling failures.
- Engine policy decides whether a failed artifact or projection can be resumed or must be
  rebuilt.
- Runtime chooses the orchestration retry and timeout policy for the deployment.
- PostgreSQL publication stays an explicit, atomic operation after verification.

The project deliberately publishes no universal worker counts, timeouts or retry numbers.
Those values depend on document size, provider quotas, model latency and the resources each
deployment assigns.

## Safe operator controls

A source ingestion workflow answers status and progress queries and accepts pause, resume and
graceful-cancel signals. From the CLI these are `harborrag ingestions get`, `get --watch`,
`pause`, `resume` and `cancel`; see
[Run and control ingestion](../../users/cli-reference/README.md#run-and-control-ingestion).

A pause stops scheduling new expensive work after the current safe boundary. HarborRAG
pauses a run by signal, not with Temporal's native pause, which can hang a run on Temporal
1.31, so the Temporal UI shows a paused run as Running. The Current Details of its
`harborrag-source:<ingestion_id>` workflow say what HarborRAG is doing:
`Paused by HarborRAG at <time>; resume with POST /v1/admin/ingestions/<ingestion_id>/resume`
while it is paused, `Cancelling at HarborRAG's request since <time>` while a cancel is
pending, with `<time>` in UTC, and nothing while it runs. Batch and document workflows set
none.

A graceful cancellation starts no further document and cancels the document workflows in
flight, which record themselves as `document_release_cancelled` failures, so the run ends
within seconds and frees its connection. A document that is publishing is the exception:
publishing has no heartbeat, so the document waits for the publish to end and records the
version as published. A cancel during discovery cancels the discovery activity, which hears of
it at its next heartbeat that reaches Temporal: about a minute, the smaller of 0.8 times its
2-minute heartbeat timeout and the Worker's 60-second heartbeat throttle. The run then ends
cancelled without a plan. A cancellation keeps the immutable artifacts already written and
prevents an unverified version from becoming active.

Runs and batches recorded before the `harborrag-source-prompt-discovery-cancel` and
`harborrag-batch-prompt-cancel` patches keep the earlier behaviour: discovery runs to its
end, and the documents in flight finish.

A cancelled document's capture, parse or release-stage activity can keep writing until its
own next heartbeat. The index entries it leaves for its failed version never reach search
results, because retrieval checks every candidate against the active document version.

Forcefully terminating infrastructure is different from cancelling in the domain sense.
Workers must be able to restart, replay history and continue from idempotent effects, without
treating process memory as durable state.

## Failure behavior

| Failure | Expected safety outcome |
| --- | --- |
| Connector or parser fails | Version stays inactive; diagnostic state is kept |
| Artifact write is retried | Stable artifact identity prevents duplicate logical evidence |
| One projection fails | Publication does not advance until the required projections verify |
| Worker restarts | Workflow history replays and resumes at activity boundaries |
| Old projection cleanup fails | Active version stays valid; cleanup is retried separately |
| Vector store returns a stale version | Retrieval removes it through the authority check |
| Graph store is unavailable | Policy can return vector evidence without claiming graph context |

## Deployment responsibilities

The repository ships local service compositions and runnable API, worker, CLI and MCP
surfaces. A production operator still has to define:

- authentication, authorization, TLS and network trust boundaries;
- secret storage, rotation and least-privilege provider credentials;
- database and object-store backup, restore, retention and disaster recovery;
- the Temporal namespace, one per deployment, task queues, worker rollout and replay
  compatibility policy;
- resource limits, autoscaling, provider quotas, alerting and incident response;
- tenant-isolation acceptance tests suited to the deployment.

Start with [Deployment](../deployment/README.md), and validate the
[Data Lifecycle](data-lifecycle.md) invariants before you expose a service to untrusted
traffic.
