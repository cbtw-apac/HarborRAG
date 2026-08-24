# Deployment Notes

Compose stacks under `compose/`, container images under `docker/`, and
placeholders for cloud IaC under `aws/`. This file tracks deployment
constraints that aren't obvious from any single file.

## The API must run as exactly one process (for now)

The Control Plane API's live event stream (SSE ingestion progress) and its
background progress bridge are built on `InProcessEventBus`
(`harborrag_runtime.events.in_process`): an in-memory, per-process pub/sub.
That has two consequences for how the API is deployed:

- **Do not pass `--workers N` to uvicorn**, and **do not run more than one
  replica of the `api` service** behind a shared endpoint. `Dockerfile.api`
  and `compose/docker-compose.yml` already reflect this (a single `api`
  service, no `--workers` flag) -- keep it that way until an out-of-process
  event bus (Redis pub/sub) replaces `InProcessEventBus`.
- **Why it matters if violated:** a client connected to process A never sees
  a live event published by process B -- it only gets the DB-backed backlog
  on its next reconnect, so ingestion progress would appear to stall for
  roughly half of connected clients under two replicas. The `api/app.py`
  lifespan mitigates the *data-correctness* half of this on its own: the
  ingestion progress bridge tick (`AppService.sync_ingestion_progress`) is
  gated by a database-backed lease (control-plane migration `0017`,
  `singleton_leases`), so even if the process constraint is violated by
  accident, only one process's bridge actually ticks and appends events --
  the others no-op rather than each independently diffing the same tasks and
  racing each other into duplicate progress rows. It does **not** fix the
  SSE fan-out gap above: a client stuck on a non-leader process still won't
  see live events, only backlog on reconnect.

Horizontal scaling of the API (multiple processes/replicas, each seeing
every live event) is deferred until a Redis-backed `EventBusPort`
implementation lands. Until then, scale the *ingestion worker* (Temporal
worker, separately replicable already) instead of the API.
