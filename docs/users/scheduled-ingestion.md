# Scheduled Ingestion

HarborRAG manages recurring source ingestion with Temporal Schedules. Define
long-lived schedules in version-controlled YAML. Use the Temporal UI to inspect
executions and for emergency administration; avoid editing config-owned schedules there because
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

## API-Owned Schedules

The `/v1/schedules` routes are removed in 2.0.0 and return `404`. Declare
schedules in `config/schedules.yaml`; the API reconciles them at start-up.
Schedules created through those routes in 1.x keep running, because a sync
only deletes config-owned schedules; pause or delete them in the Temporal UI.

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
recording an outcome; find those in the Temporal UI. A `paused` change in
`config/schedules.yaml` records the pause as intended, so only pauses made
elsewhere — `pause_on_failure`, or the Temporal UI — raise the
unexpected-pause alert.

Relevant focused tests:

```bash
pytest packages/harborrag-runtime/tests/runtime_ingestion/unit/scheduling
pytest packages/harborrag-app/tests/test_api_schedules.py
```
