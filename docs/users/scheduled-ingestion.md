# Scheduled Ingestion

HarborRAG manages recurring source ingestion with Temporal Schedules. Define
long-lived schedules in version-controlled YAML or manage API-owned schedules
through the authenticated API. Use the Temporal UI to inspect executions and for
emergency administration; avoid editing config-owned schedules there because
the next config sync will overwrite those changes. Pause state is the exception:
a sync never pauses or unpauses an existing schedule, so a pause applied during
an incident survives restarts until someone unpauses it.

## Configure a Schedule

Copy `config/schedules.example.yaml` to `config/schedules.yaml`. API startup
creates missing schedules and updates existing config-owned schedules. Each
`connection_id` must identify an enabled connector in
`config/connectors.yaml`.

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

Use either or both of `cron` and `interval_seconds`. Cron schedules accept five
fields and default to UTC when `timezone` is omitted. Intervals are measured in
seconds and align to UTC/epoch time; for local wall-clock execution, use cron.
Optional `jitter_seconds` spreads starts across a bounded random delay.

Policy defaults are `overlap: skip`, a 3600-second catch-up window, and
`pause_on_failure: false`. Catch-up windows must be at least 10 seconds. `skip`
prevents a schedule from starting another run while its prior run is active;
other supported Temporal overlap policies are `buffer_one`, `buffer_all`,
`cancel_other`, `terminate_other`, and `allow_all`. Use overlapping policies
only when the ingestion target can tolerate concurrent runs.

Set `prune: true` only when removing a config-owned schedule from the YAML file
should delete it from Temporal. The default `false` avoids deleting schedules
because of an incomplete deployment file.

## Manage API-Owned Schedules

API-owned schedules can be created and updated without editing YAML. Requests
require the `editor` role for writes and tenant access; reads require the
`reader` role.

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

Use `PATCH /v1/schedules/{schedule_id}` to update an API-owned schedule. Omit
`paused` to keep the schedule's current pause state; send `true` or `false` (with
an optional `note`) to pause or unpause it as part of the update. A
schedule ID must be unique. A duplicate create returns a conflict with guidance
to choose a different ID or update the existing schedule. Config-owned schedules
are read-only through create/update API calls; edit their YAML instead.

Available operations:

- `GET /v1/schedules?tenant=DEFAULT` lists schedules visible to the caller.
- `GET /v1/schedules/{schedule_id}` describes next run times, recent actions,
  total runs, and overlap skips.
- `POST /v1/schedules/{schedule_id}/pause` pauses the schedule. Send a note to
  record the reason, for example `{"note":"investigating connector outage"}`.
- `POST /v1/schedules/{schedule_id}/unpause` unpauses it; an optional note records
  why it was unpaused.
- `POST /v1/schedules/{schedule_id}/trigger` requests an immediate run.
- `POST /v1/schedules/{schedule_id}/backfill` runs actions for an explicit
  timezone-aware `start_at` and `end_at` range.
- `DELETE /v1/schedules/{schedule_id}` deletes an API-owned schedule.

Backfill is implemented. Confirm the desired backfill behavior and operational
limits with the product owner before using it for large historical ranges.

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
state. The monitoring Compose stack scrapes `temporal-worker:9464` every 15
seconds. Open `http://localhost:9090/graph` to query metrics and
`http://localhost:9090/alerts` to inspect firing alerts. The rules alert on
failed scheduled runs and schedules paused unexpectedly. Notification delivery
requires an Alertmanager receiver; this repository's rules alone do not send
email or chat messages.

Relevant focused tests:

```bash
pytest packages/harborrag-runtime/tests/runtime_ingestion/unit/scheduling
pytest packages/harborrag-app/tests/test_api_schedules.py
```