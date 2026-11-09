# Scheduled ingestion

Run a connection's ingestion on a timetable, such as every night at 1:00. You declare
schedules in `config/sources/schedules.yaml`, and the API applies the file to
[Temporal Schedules](https://docs.temporal.io/schedule) each time it starts. A
connection is one configured data source, and its `source_key` is the name schedules use
for it; both are defined in [Connections](connections.md#terms-used-on-this-page).

Use the Temporal UI to inspect runs and for emergency administration, but do not edit a
schedule that the file declares there: the next sync overwrites the change. Pause state
is the exception. A sync pauses or unpauses an existing schedule only when its `paused`
value in the file changes, so a pause applied during an incident survives restarts
until someone unpauses it.

## Before you start

- The full local stack, with the API, Temporal and an ingestion worker running; see the
  [Quick Start](../getting-started/quick-start.md).
- The connection to schedule and its `source_key`. A connection imported from
  `config/sources/connectors.yaml` uses its YAML key, such as `harborrag-workspace`; a
  connection created through the API or `harborrag source add`, or one that existed
  before database migration 0041, uses `PROJECT/CONNECTION`, such as
  `default/harborrag-workspace`.

## Declare a schedule

1. Copy the example file. The repository ignores `config/sources/schedules.yaml`, so
    keep your copy under your deployment's own version control:

    ```bash
    cp config/sources/schedules.example.yaml config/sources/schedules.yaml
    ```

2. Edit the copy. This schedule starts an incremental ingestion of
    `harborrag-workspace` every day at 1:00 in the `Asia/Ho_Chi_Minh` time zone:

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

    `source.connection_id` is the connection's `source_key` in `source.tenant`, not its
    `PROJECT/CONNECTION` address. Leave `source_scope_id: null` so scheduled runs use the
    same source scope as a run started with `POST /v1/admin/ingestions`; a different
    scope is ingested and tracked separately.

3. Restart the API, for example with `scripts/deployment/dev.sh down` and then
    `scripts/deployment/dev.sh up`. The API reads the file only at startup. The Compose
    stack mounts `config/` into the API container read-only, so no image rebuild is
    needed. The API reads the file from `HARBORRAG_SCHEDULE_CONFIG_PATH`, which defaults
    to `config/sources/schedules.yaml` relative to the working directory and is set to
    `/app/config/sources/schedules.yaml` in the API image and the Compose stack.

## Check that the schedule exists

The API log reports each sync:

```bash
docker logs harborrag-api-api-1 2>&1 | grep -i schedule
```

A successful first sync of one schedule logs
`Declared schedules synced created=1 updated=0 deleted=0 failed=0`; later restarts count it
under `updated`. The schedule appears under **Schedules** in the Temporal UI, at
`http://localhost:8080` on the local stack. Other outcomes:

- `No declarative schedule file at …; skipping schedule sync`: the API found no file at
  `HARBORRAG_SCHEDULE_CONFIG_PATH`.
- `Declared schedule sync failed; fix the deployment and restart`: the file is invalid,
  for example an unknown key, a duplicate `id` or a bad cron expression. One invalid
  entry stops the whole file, and no schedule changes.
- `Declared schedule <id> was not applied: …`: that schedule was skipped and the others
  were applied. The usual cause is a `connection_id` that does not exist in
  `source.tenant`.
- While Temporal is unreachable, the API keeps retrying the sync in the background, and
  its own startup is not blocked.

## Schedule file reference

The file has `version: 1`, an optional `prune` and a `schedules` list. Unknown keys are
rejected.

| Key | Default | Meaning |
| --- | --- | --- |
| `prune` | `false` | `true` deletes a schedule from Temporal when it is removed from the file. Only schedules the file created are deleted. The default avoids deleting schedules because of an incomplete deployment file. |
| `id` | required | 1 to 128 letters, digits, `.`, `_` or `-`, starting with a letter or digit; unique in the file. |
| `workflow` | required | `source_ingestion`, the only supported value. |
| `cron` | none | Five fields (minute, hour, day of month, month, day of week), or one of `@yearly`, `@annually`, `@monthly`, `@weekly`, `@daily`, `@midnight`, `@hourly`. Set `cron`, `interval_seconds`, or both. |
| `interval_seconds` | none | Seconds between runs, aligned to UTC and the Unix epoch. For local wall-clock times, use `cron`. |
| `timezone` | `UTC` | IANA time zone for `cron`. A schedule with only `interval_seconds` must stay in `UTC`. |
| `overlap` | `skip` | What happens when a firing comes while the previous run is still active: `skip` drops it, `buffer_one` queues one, `buffer_all` queues every one. |
| `catchup_window_seconds` | `3600` | How late a missed firing may still start, for example after Temporal was down. At least 10 seconds. |
| `jitter_seconds` | none | Spreads starts across a random delay of up to this many seconds. |
| `pause_on_failure` | `false` | `true` pauses the schedule after a failed run. |
| `paused` | `false` | Declared pause state, applied on creation and whenever the value changes in the file. |
| `note` | none | Free text kept with the schedule, up to 1,000 characters. |
| `source.tenant` | `DEFAULT` | Tenant that owns the connection. |
| `source.connection_id` | required | The connection's `source_key`. |
| `source.source_scope_id` | `null` | Source scope to ingest; leave it `null`. |
| `source.mode` | `incremental` | `incremental` skips unchanged documents; `force` evaluates every discovered document again. See [Ingestion modes](ingestion-modes.md). |

Intervals, jitter and catch-up windows are capped at 366 days. A source scope runs one
authoritative scan at a time, so the concurrent (`allow_all`) and cancelling
(`cancel_other`, `terminate_other`) overlap policies are not supported.

## How a scheduled run works

Each firing starts the `harborrag.scheduled_source_ingestion` workflow on the configured
discovery task queue, which starts the normal `harborrag.source_ingestion` workflow as a
child. A firing does not replay an earlier Temporal run or discover records that the
connection's query excludes.

- Each firing reads the connection from the control database, so a change to the
  connection applies from the next firing, with no restart. Changes to the worker's own
  configuration, such as its task queues, apply after the workers restart.
- When the connection was removed, is being deleted or holds invalid values, the firing
  fails at once and is not retried.
- When another ingestion of the same source scope is already running, such as a manual
  ingestion or another schedule, the firing is skipped and recorded with outcome
  `skipped` instead of failing. Repeated skips with no completed run mean the scope is
  held by a long-running or stranded scan; the `HarborRAGScheduleKeepsSkipping` alert
  reports this after a day.

## Schedules created in 1.x

The `/v1/schedules` routes were removed in 2.0.0 and return `404`. Schedules created
through them keep running, because a sync only deletes schedules the file created.
Pause or delete them in the Temporal UI.

## Monitor scheduled runs

Temporal appends each scheduled action time to the workflow ID, and sets the built-in
`TemporalScheduledById` and `TemporalScheduledStartTime` search attributes, which you can
filter on in the Temporal UI.

Worker logs include the schedule ID, workflow ID and run ID for scheduled starts, child
starts, completions and failures. On the local stack, follow them with:

```bash
docker logs -f harborrag-temporal-temporal-worker-1
```

Run outcomes are `started`, `completed`, `partial`, `skipped`, `cancelled` and
`failed`. A run that is terminated, or hits its execution timeout, stops without
recording an outcome; find those in the Temporal UI.

Worker Prometheus metrics include scheduled run outcomes, schedule-to-start latency,
overlap skips, and actual versus configured pause state. Each worker describes every
schedule every `HARBORRAG_SCHEDULE_METRICS_INTERVAL_SECONDS` (default 30) to export the
pause and overlap gauges; with several worker replicas, set it to `0` on all but one to
avoid duplicate Temporal load and series.

The monitoring Compose stack scrapes `temporal-worker:9464` every 15 seconds. Open
`http://localhost:9090/graph` to query metrics and `http://localhost:9090/alerts` to see
firing alerts. The alert rules cover:

| Alert | Fires when |
| --- | --- |
| `HarborRAGScheduledWorkflowFailed` | A scheduled run failed. |
| `HarborRAGScheduleUnexpectedlyPaused` | A schedule is paused although the file does not declare it paused, for example by `pause_on_failure` or the Temporal UI. A `paused` change in the file records the pause as intended and does not fire it. |
| `HarborRAGScheduleKeepsSkipping` | A schedule skipped runs for a day without completing one. |

The rules alone send no email or chat message: notification delivery needs an
Alertmanager receiver; see Prometheus's
[Alertmanager configuration](https://prometheus.io/docs/alerting/latest/configuration/).
