# Scheduled Ingestion

HarborRAG manages recurring source ingestion with Temporal Schedules. Define
long-lived schedules in version-controlled YAML or manage API-owned schedules
through the authenticated API. Use the Temporal UI to inspect executions and for
emergency administration; avoid editing config-owned schedules there because
the next config sync will overwrite those changes. Pause state is the exception:
a sync only pauses or unpauses an existing schedule when its `paused` value in
the YAML file changes, so a pause applied during an incident survives restarts
until someone unpauses it.

## Configure a Schedule

Copy `config/schedules.example.yaml` to `config/schedules.yaml`. API startup
creates missing schedules and updates existing config-owned schedules. Each
`connection_id` must identify an enabled connector in
`config/connectors.yaml`. The API reads the file from
`HARBORRAG_SCHEDULE_CONFIG_PATH` (default `config/schedules.yaml`, relative to
the working directory); the API image and Compose stack set it to
`/app/config/schedules.yaml`.

Leave `source_scope_id: null` so scheduled runs use the same source scope that
`POST /v1/ingestions` derives for the connector; a different scope is ingested
and tracked separately.

This example starts an incremental workspace ingestion every day at 1:00 AM in
Vietnam:

```yaml
version: 1
prune: false

schedules:
  - id: workspace-daily-ingest
    workflow: source_ingestion
    cron: "0 1 * * *"
    timezone: Asia/Ho_Chi_Minh
    overlap: skip
    catchup_window_seconds: 3600
    jitter_seconds: 30
    pause_on_failure: false
    paused: false
    source:
      tenant: DEFAULT
      connection_id: harborrag-workspace
      source_scope_id: null
      mode: incremental
```

`workflow: source_ingestion` is HarborRAG's supported workflow value. HarborRAG
creates the Temporal workflow type `harborrag.scheduled_source_ingestion` on the
configured discovery task queue. `incremental` skips unchanged documents;
`force` reprocesses unchanged documents discovered from that connector. The
schedule does not replay an arbitrary previous Temporal run or discover records
excluded by the connector query.

Each firing prepares its source on the worker from the configuration the
worker loaded, so connector, processing and task-queue changes apply to the
next firing after the workers restart, without recreating the schedule. When
another ingestion of the same source scope is already running (a manual
ingestion or another schedule), the firing is skipped and recorded with outcome
`skipped` instead of failing. Repeated skips with no completed run mean the
scope is held by a long-running or stranded scan; the
`HarborRAGScheduleKeepsSkipping` alert reports this after a day.

Use either or both of `cron` and `interval_seconds`. Cron schedules accept five
fields and default to UTC when `timezone` is omitted. Intervals are measured in
seconds and align to UTC/epoch time; for local wall-clock execution, use cron.
Optional `jitter_seconds` spreads starts across a bounded random delay.
Intervals, jitter and catch-up windows are capped at 366 days.

Policy defaults are `overlap: skip`, a 3600-second catch-up window, and
`pause_on_failure: false`. Catch-up windows must be at least 10 seconds. `skip`
prevents a schedule from starting another run while its prior run is active;
`buffer_one` and `buffer_all` queue one or every overlapping firing to run after
it. A source scope runs one authoritative scan at a time, so concurrent
(`allow_all`) and cancelling (`cancel_other`, `terminate_other`) policies are
not supported.

Set `prune: true` only when removing a config-owned schedule from the YAML file
should delete it from Temporal. The default `false` avoids deleting schedules
because of an incomplete deployment file.

## Manage API-Owned Schedules

API-owned schedules can be created and updated without editing YAML. Requests
require the `editor` role for writes and tenant access; reads require the
`reader` role. Creating or updating a schedule that names a `source_scope_id`
(or updating one that already has one) requires the `admin` role, as it does for
`POST /v1/ingestions`. Schedules in tenants outside the caller's access return
`404`.

```http
POST /v1/schedules
```

```json
{
  "schedule_id": "workspace-daily-ingest",
  "workflow": "source_ingestion",
  "cron": "0 1 * * *",
  "timezone": "Asia/Ho_Chi_Minh",
  "overlap": "skip",
  "catchup_window_seconds": 3600,
  "jitter_seconds": 30,
  "pause_on_failure": false,
  "source": {
    "tenant": "DEFAULT",
    "connection_id": "harborrag-workspace",
    "source_scope_id": null,
    "mode": "incremental"
  },
  "paused": false,
  "note": "Daily workspace ingestion"
}
```

Use `PATCH /v1/schedules/{schedule_id}` to update an API-owned schedule. PATCH
is a partial update: send only the fields to change (for example
`{"cron": "0 2 * * *"}`, or `{"source": {"mode": "force"}}`); omitted fields keep
their current value and an explicit `null` clears an optional field. The ID,
workflow and tenant cannot change. Omit `paused` to keep the schedule's current
pause state; send `true` or `false` (with an optional `note`) to pause or
unpause it as part of the update. Schedule IDs are global across tenants, and
IDs declared in `config/schedules.yaml` are reserved. Creating a schedule with
an ID that is already taken, in any tenant, returns `409
SCHEDULE_ALREADY_EXISTS` saying the ID is unavailable; choose another ID.
Config-owned schedules are read-only through create/update API calls; edit their
YAML instead.

Available operations:

- `GET /v1/schedules?tenant=DEFAULT` lists schedules visible to the caller.
- `GET /v1/schedules/{schedule_id}` describes next run times, recent actions,
  total runs, and overlap skips.
- `POST /v1/schedules/{schedule_id}/pause` pauses the schedule. Send a note to
  record the reason, for example `{"note":"investigating connector outage"}`.
- `POST /v1/schedules/{schedule_id}/unpause` unpauses it; an optional note records
  why it was unpaused.
- `POST /v1/schedules/{schedule_id}/trigger` requests an immediate run. The
  schedule's overlap policy still applies, so with `skip` the request is dropped
  while a run is active.
- `POST /v1/schedules/{schedule_id}/backfill` runs the actions that would have
  fired in an explicit timezone-aware `start_at`–`end_at` range of at most 31
  days. Each run ingests the source as it is now, not as it was at that time,
  and the schedule's overlap policy applies, so with `skip` a backfill usually
  results in one run.
- `DELETE /v1/schedules/{schedule_id}` deletes an API-owned schedule.

## Monitor and Troubleshoot

Scheduled runs execute the `harborrag.scheduled_source_ingestion` wrapper, which
starts the existing `harborrag.source_ingestion` workflow as a child. Temporal
appends each scheduled action timestamp to its workflow ID, and supplies the
built-in `TemporalScheduledById` and `TemporalScheduledStartTime` search
attributes for filtering executions.

Worker logs include schedule ID, workflow ID, and run ID for scheduled starts,
child starts, completions, and failures. With the Temporal Compose stack, follow
the worker logs with:

```bash
docker compose -f deploy/compose/docker-compose.temporal.yml logs -f temporal-worker
```

Worker Prometheus metrics include scheduled run outcomes,
schedule-to-start latency, overlap skips, and actual versus configured pause
state. Each worker describes every schedule every
`HARBORRAG_SCHEDULE_METRICS_INTERVAL_SECONDS` (default 30) to export the pause
and overlap gauges; with several worker replicas, set it to `0` on all but one
to avoid duplicate Temporal load and series. The monitoring Compose stack scrapes `temporal-worker:9464` every 15
seconds. Open `http://localhost:9090/graph` to query metrics and
`http://localhost:9090/alerts` to inspect firing alerts. The rules alert on
failed scheduled runs, schedules paused unexpectedly, and schedules that keep
skipping their runs. Notification delivery
requires an Alertmanager receiver; this repository's rules alone do not send
email or chat messages.

Run outcomes are `started`, `completed`, `partial`, `skipped`, `cancelled` and
`failed`. A run that is terminated or hits its execution timeout stops without
recording an outcome; find those in the Temporal UI. Pausing through the API
(`POST .../pause` or `PATCH` with `paused`) records the pause as intended, so
only pauses made elsewhere — `pause_on_failure`, or the Temporal UI — raise the
unexpected-pause alert.

Relevant focused tests:

```bash
pytest packages/harborrag-runtime/tests/runtime_ingestion/unit/scheduling
pytest packages/harborrag-app/tests/test_api_schedules.py
```