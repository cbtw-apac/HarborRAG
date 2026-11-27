# Scheduled ingestion

Run a connection's ingestion on a timetable, such as every hour or every night at 1:00.
Create and manage schedules over HTTP with `/v1/admin/schedules`: the control database
stores each schedule, and [Temporal](https://docs.temporal.io/schedule) fires its
slots. A slot is one time a schedule comes due.

Schedules can also be declared in `config/sources/schedules.yaml`, which the API applies
each time it starts; see [Declare schedules in a file](#declare-schedules-in-a-file). The
two kinds are separate: `/v1/admin/schedules` lists and changes only the schedules
created through it.

## Before you start

- The full local stack, with the API, Temporal and an ingestion worker running; see the
  [Quick Start](../getting-started/quick-start.md). The API fires the slots, so it needs
  Temporal as well as the workers do.
- The API's address. The examples use the local stack's `http://127.0.0.1:8000`.
- A bearer token in `$TOKEN` whose role is `editor` or higher; see
  [Configure API authentication](configuration/workspace-mode.md#configure-api-authentication).
  On the local stack, `env/.env.api` sets `HARBORRAG_AUTH_MODE=none`, which treats every
  request as `owner`, so leave out the `Authorization` header there. A credential that
  holds several tenants also sends `X-Tenant-Id`.
- The connection to schedule, named by its `project_id` and `connection_id`. The examples
  use the connection `demo/demo-tickets`; replace it with one of yours, which
  `GET /v1/admin/connections` or `harborrag connections list` lists. Terms such as
  connection and connection ref are defined in
  [Connections](connections.md#terms-used-on-this-page).

## Create a schedule

`PUT /v1/admin/schedules/{schedule_id}` creates the schedule that `schedule_id` names.
This one starts an incremental ingestion of `demo/demo-tickets` every hour, each slot up
to 5 minutes late at random:

```bash
curl -X PUT -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -H "If-None-Match: *" \
  http://127.0.0.1:8000/v1/admin/schedules/demo-tickets-hourly \
  -d '{
    "description": "Changed DEMO tickets every hour",
    "input": {"project_id": "demo", "connection_id": "demo-tickets", "mode": "incremental"},
    "interval": "PT1H",
    "jitter": "PT5M"
  }'
```

The answer is `201` with the schedule and an `ETag` header, such as `W/"rev-1"`.
`If-None-Match: *` makes the call create-only: when the ID is taken, the answer is
`409 already_exists` and nothing changes. Without the header, the same call creates the
schedule or overwrites an existing one with `200`.

A schedule ID is chosen by you and unique in the tenant: 1 to 63 lowercase letters,
digits, `_` or `-`, starting with a letter or a digit (`^[a-z0-9][a-z0-9_-]{0,62}$`).

A `cron` timetable runs at wall-clock times in a time zone. This schedule re-evaluates
every document of the same connection each Sunday at 2:00 in Ho Chi Minh City, and waits
for a run already in progress instead of skipping the slot:

```bash
curl -X PUT -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -H "If-None-Match: *" \
  http://127.0.0.1:8000/v1/admin/schedules/demo-tickets-weekly-full \
  -d '{
    "description": "Every DEMO ticket again each Sunday",
    "input": {"project_id": "demo", "connection_id": "demo-tickets", "mode": "force"},
    "cron": "0 2 * * 0",
    "timezone": "Asia/Ho_Chi_Minh",
    "if_running": "queue"
  }'
```

### Schedule fields

The body of the `PUT` is the whole schedule. Unknown fields are refused.

| Field | Default | Meaning |
| --- | --- | --- |
| `description` | none | Free text, up to 200 characters. |
| `operation` | `start_ingestion` | The operation each slot calls; `start_ingestion` is the only value. |
| `input.project_id`, `input.connection_id` | required | The connection to ingest. |
| `input.mode` | `incremental` | `incremental` skips unchanged documents; `force` evaluates every document again. See [Ingestion modes](ingestion-modes.md). `input` takes no other field, so `item_refs` and `updated_after` are refused. |
| `interval` | none | Time between slots as an ISO 8601 duration, such as `PT1H`: at least `schedules.min_interval` (`PT15M` unless changed) and at most `P366D`. Set exactly one of `interval` and `cron`. |
| `cron` | none | Five fields (minute, hour, day of month, month, day of week), or a macro such as `@hourly` or `@daily`. No two runs may come closer together than `schedules.min_interval`. |
| `timezone` | `UTC` | IANA time zone that `cron` runs in, such as `Asia/Ho_Chi_Minh`. |
| `jitter` | `PT0S` | Delays each slot at random by up to this long: at most half the interval, or half the shortest gap between `cron` runs, and at most `PT1H`. |
| `if_running` | `skip` | What a slot does when the connection already has a pending, running or paused run: `skip` records the slot as skipped; `queue` waits and starts the run when that one ends. One slot waits at most: a later slot is skipped while one waits, and a waiting slot gives up after 24 hours. |
| `catch_up` | `false` | How late a slot may still run, for example after an outage of Temporal or of every API replica: `false` allows less than a minute; `true` allows the interval, or the shortest gap between `cron` runs, 24 hours at most. `jitter` adds to both. A later slot is dropped and leaves no record. When several missed slots fall within the window, the first starts a run and the others are skipped or queued as `if_running` says. |
| `pause_after_failures` | `3` | Pauses the schedule after this many failed runs in a row, 0 to 20; `0` never pauses it. |

The check of the gap between `cron` runs reads only the minute and hour fields, so a gap
that only the day fields create is not measured, and neither is a shift to or from
daylight saving time.

### When a `PUT` is refused

Nothing is stored when a `PUT` is refused.

| Status | Cause |
| --- | --- |
| `422 validation_error` | A field breaks the schema or a timetable rule. `details` lists each broken rule as `{field, issue}`: an `interval` under `schedules.min_interval` or over `P366D`; a `cron` expression that is invalid or runs closer together than `schedules.min_interval`; an unknown `timezone`; a `jitter` over its limit; both or neither of `interval` and `cron`; or `input.connection_id` when the connection already has `schedules.max_per_connection` schedules (5 unless changed). |
| `422 reference_not_found` | The project or the connection does not exist; `details.field` is `input.project_id` or `input.connection_id`. |
| `409 invalid_state` | The connection is being deleted, including when its deletion began during the `PUT`. |
| `409 already_exists` | `If-None-Match: *` and the ID is taken. |
| `412 precondition_failed` | `If-Match` does not match the schedule's current `ETag`, or `If-Match: *` names a schedule that does not exist. |
| `503 dependency_unavailable` | Temporal cannot be reached. A new schedule is not created, and a stored one keeps its previous configuration. Send the same `PUT` again once Temporal answers. |

## Check a schedule

`GET /v1/admin/schedules/{schedule_id}` returns the schedule with its last 20 slots,
newest first, and its `ETag`:

```bash
curl -i -H "Authorization: Bearer $TOKEN" \
  http://127.0.0.1:8000/v1/admin/schedules/demo-tickets-hourly
```

Besides the fields you set, the schedule carries:

| Field | Meaning |
| --- | --- |
| `connection_ref` | `project_id/connection_id` from `input`. |
| `state` | `active` or `paused`. |
| `paused` | `null` while active; otherwise `reason`, `by` (the subject who paused it, or `system` for an automatic pause) and `at`. |
| `next_run_at` | When the next slot fires; `null` while the schedule is paused or while `schedules.enabled` is `false`. |
| `consecutive_failures` | Failed runs in a row, counted toward `pause_after_failures`. |
| `recent_slots` | The latest slots, skipped ones included: `due_at`, `outcome`, `reason`, `manual` (`true` for a slot that `trigger` started) and `run` (`type`, `id` and current `status` of the run involved, or `null`). |
| `created_by`, `updated_by`, `created_at`, `updated_at` | Who wrote the schedule, read from the token, and when. |

Each slot's `outcome` and `reason`:

| `outcome` | `reason` | What happened |
| --- | --- | --- |
| `started` | `null` | The slot started the run in `run`. |
| `skipped` | `already_running` | The connection had the run in `run`, and `if_running` is `skip`, or another slot was already waiting. |
| `queued` | `already_running` | The slot waits for the run in `run` to end. When it starts its own run, a `started` slot with the same `due_at` is recorded. |
| `skipped` | `disabled_by_setting` | `schedules.enabled` is `false`. |
| `skipped` | `connection_deleting` | The connection is being deleted. |
| `skipped` | `reference_not_found` | The project or the connection no longer exists. |
| `skipped` | `null` | The run's checks refused it, for example because the connection's stored settings no longer match its source type. The slot counts as a failed run toward `pause_after_failures`, and the API logs `Schedule slot could not start a run schedule_id=<schedule_id> error_type=<error_type>`. |

A paused schedule fires no slots and records none, and a slot dropped after its catch-up
window records nothing either.

## List schedules

`GET /v1/admin/schedules` lists the tenant's schedules, newest first. Each item carries
only its latest slot in `recent_slots`; read one schedule for the last 20.

```bash
curl -H "Authorization: Bearer $TOKEN" \
  "http://127.0.0.1:8000/v1/admin/schedules?connection_ref=demo/demo-tickets&state=active"
```

| Parameter | Meaning |
| --- | --- |
| `project_id` | Schedules of one project. |
| `connection_ref` | Schedules of one connection, as `project_id/connection_id`. |
| `operation` | `start_ingestion`. |
| `state` | `active` or `paused`. |
| `due_before` | Schedules whose `next_run_at` is earlier than this date-time, which needs a UTC offset, such as `2026-12-01T00:00:00Z`. |
| `limit`, `cursor` | Page size, 1 to 100 (20 by default), and the `next_cursor` of the previous page. |

## Change a schedule

Read the schedule, then send the whole changed body with its `ETag` in `If-Match`:

```bash
curl -X PUT -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -H 'If-Match: W/"rev-1"' \
  http://127.0.0.1:8000/v1/admin/schedules/demo-tickets-hourly \
  -d '{
    "description": "Changed DEMO tickets every two hours",
    "input": {"project_id": "demo", "connection_id": "demo-tickets", "mode": "incremental"},
    "interval": "PT2H",
    "jitter": "PT5M"
  }'
```

The answer is `200` with the new `ETag`. A field you leave out returns to its default.
An overwrite keeps the schedule's state, pause, failure count and slots, and the new
timetable applies from the next slot. When someone else changed the schedule after your
read, the answer is `412 precondition_failed`: read it again and retry. `If-Match: *`
overwrites whatever version is stored, but never creates.

## Pause, resume and trigger a schedule

Pause a schedule, for example during a source's maintenance window:

```bash
curl -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  http://127.0.0.1:8000/v1/admin/schedules/demo-tickets-hourly/pause \
  -d '{"reason": "Jira maintenance window"}'
```

The body is optional, and `reason` holds up to 200 characters; without one, the reason is
`Paused by an operator`. The answer is `200` with the schedule, whose `paused` records
the reason, who paused it and when. Pausing a paused schedule returns it unchanged, and a
run already in progress keeps going.

Resume it:

```bash
curl -X POST -H "Authorization: Bearer $TOKEN" \
  http://127.0.0.1:8000/v1/admin/schedules/demo-tickets-hourly/resume
```

Resuming clears the pause and `consecutive_failures`, and answers `200` with the
schedule. Resuming an active schedule changes nothing. Either way, resuming writes the
schedule's Temporal Schedule again, so it also recreates one that went missing, for
example after a Temporal namespace was reset.

Pause and resume also answer the schedule's `ETag`, ready for the `If-Match` of a later
`PUT`. A pause or resume that changes the schedule advances it, such as from
`W/"rev-2"` to `W/"rev-3"`; one that changes nothing returns it unchanged.

Start one ingestion now with the schedule's input, even while the schedule is paused or
`schedules.enabled` is `false`:

```bash
curl -X POST -H "Authorization: Bearer $TOKEN" \
  http://127.0.0.1:8000/v1/admin/schedules/demo-tickets-hourly/trigger
```

The answer is `202` with the run, as `POST /v1/admin/ingestions` returns it, and a
`Location` header such as `/v1/admin/ingestions/{ingestion_id}`. The slot is recorded
with `manual: true`, and the run counts toward `pause_after_failures` like a scheduled
one. A trigger is refused with:

- `409 already_running` when the connection has a run, which `details.ingestion_id`
  names;
- `409 invalid_state` while the connection is being deleted;
- `422 reference_not_found` when the project or the connection no longer exists, with
  `details.field` set to `project_id` or `connection_id`.

## Delete a schedule

```bash
curl -X DELETE -H "Authorization: Bearer $TOKEN" \
  http://127.0.0.1:8000/v1/admin/schedules/demo-tickets-hourly
```

The answer is `204`. The schedule, its slots and its Temporal Schedule are removed; a run
the schedule started keeps going. Deleting a connection, or a project with
`cascade=true`, removes the connection's schedules before its data.

## How a schedule runs

- The control database holds each schedule. Its Temporal Schedule, with the ID
  `harborrag-schedule:<tenant_id>:<schedule_id>`, only fires the slots: each slot starts
  the workflow `harborrag.schedule_slot` on the task queue `harborrag-schedules`, or the
  one `HARBORRAG_SCHEDULE_TASK_QUEUE` names.
- A Temporal Schedule ID is unique only within its Temporal namespace, so give each
  deployment its own namespace, with `connection.namespace` in `config/temporal.yaml` or
  `HARBORRAG_TEMPORAL_NAMESPACE`. Two deployments in one namespace that use the same
  tenant and schedule ID overwrite each other's Temporal Schedule, whatever their task
  queues. A Temporal Schedule keeps the task queue it was written with, so after you
  change `HARBORRAG_SCHEDULE_TASK_QUEUE`, send each schedule's `PUT` again.
- The API process runs the worker for that queue, so every API replica polls it, and the
  ingestion workers need no change. While Temporal is unreachable, no slot fires; the API
  logs `Schedule slot worker failed; restarting in 30s` and tries again every 30 seconds.
  While Temporal runs but no API replica polls the queue, for example during an API
  restart, the slots that come due wait in Temporal. When an API returns, a slot still
  within its catch-up window runs (see `catch_up` in [Schedule fields](#schedule-fields)),
  and a later one is dropped; the API logs
  `Schedule slot fired after its catch-up window schedule_id=<schedule_id>`.
- Every write sets the Temporal Schedule too. While Temporal is unreachable, `PUT`,
  `DELETE`, pause and resume answer `503 dependency_unavailable`; send the call again once
  Temporal answers. Reading schedules does not need Temporal.
- A slot starts its run through the same code path as `POST /v1/admin/ingestions`, with
  the same checks, the same one-run-per-connection rule, the settings pinned to the run
  (see [Run pinning](configuration/runtime-settings.md#run-pinning)) and the same
  activity entry. The run's `triggered_by` is
  `{"principal_type": "schedule", "principal_id": "<schedule_id>"}`.
- After `pause_after_failures` failed runs in a row, the schedule pauses with
  `paused.by` set to `system` and the reason
  `Paused after <n> failed runs in a row (<error_category>)`, and the API logs
  `Schedule paused after failed runs schedule_id=<schedule_id> failures=<n>`. A
  succeeded run resets the count, a cancelled run leaves it as it is, and a run still
  going after 7 days is not counted. Resume the schedule once the cause is fixed.
- Do not edit these schedules in the Temporal UI: the next write from the API replaces
  the whole Temporal Schedule.

### Stop every schedule for maintenance

The tenant setting `schedules.enabled: false` records every slot as `skipped` with the
reason `disabled_by_setting`, and `next_run_at` reads `null`, until you set it back to
`true`. It needs the `admin` role:

```bash
curl -X PATCH -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -H "If-Match: *" \
  -d '{"schedules.enabled": false}' \
  "http://127.0.0.1:8000/v1/admin/settings?level=tenant"
```

`schedules.min_interval` and `schedules.max_per_connection` set the limits a `PUT`
checks; see [Runtime settings](configuration/runtime-settings.md#settings-and-their-levels).

### Monitor schedules

- `GET /v1/admin/schedules?state=paused` lists the paused schedules; `paused.by` is
  `system` for those paused after failed runs.
- `recent_slots` of a schedule shows what each slot did, and `run.id` is the
  `ingestion_id` to read with `GET /v1/admin/ingestions/{ingestion_id}`.
- The API log names the schedule in each line, such as
  `Schedule slot skipped schedule_id=<schedule_id> reason=<reason>`. On the local stack:

    ```bash
    docker logs harborrag-api-api-1 2>&1 | grep -i schedule
    ```

- `GET /v1/admin/connections?scheduled=true` lists the connections that have a schedule
  that is not paused, and `scheduled=false` the others; the filter applies before the
  page is cut. `harborrag connections list --scheduled` and `--no-scheduled` do the same.
  Each connection carries `next_run_at`, the earliest next slot among those schedules,
  which is `null` when it has none, has only paused ones, or while `schedules.enabled`
  is `false`.

The worker metrics and alerts in [Monitor declared runs](#monitor-declared-runs) do not
cover the schedules of `/v1/admin/schedules`.

### List the runs of a schedule

`recent_slots` keeps only the last 20 slots. To list every run a schedule started,
`trigger` runs included, filter the ingestion list by `schedule_id`:

```bash
curl -H "Authorization: Bearer $TOKEN" \
  "http://127.0.0.1:8000/v1/admin/ingestions?schedule_id=demo-tickets-hourly"
```

From a terminal, `harborrag ingestions list --schedule-id demo-tickets-hourly` does the
same. `trigger=schedule`, or `--trigger schedule`, lists the runs of every schedule;
`user` and `api_key` list the runs people and API keys started. A run from
`config/sources/schedules.yaml` records no starter, so it counts as `user`. A
`schedule_id` together with `trigger=user` or `trigger=api_key` gives an empty page. See
[`GET /v1/admin/ingestions`](connections.md#get-v1adminingestions) for the other
filters.

### Alert on skipped slots

The API counts every slot it records in `harborrag_schedule_slots_total`, on its own
`/metrics` at the ops port (`HARBORRAG_OPS_PORT`, `9465` by default), which answers only
to the `HARBORRAG_METRICS_SCRAPE_TOKEN` bearer token. The worker's metrics port does not
carry it. Its labels are:

- `outcome`: `started`, `skipped` or `queued`;
- `reason`: the slot's reason, such as `already_running`, or `none` for a slot without
  one, such as a started slot.

A `trigger` counts as `started` with the reason `none`. A queued slot counts once as
`queued`, then once as `started` when its run starts. Slots that record nothing, such as
those of a paused schedule or one dropped after its catch-up window, are not counted.
Each API replica counts its own slots, so add the replicas up with `sum()`.

The monitoring Compose stack scrapes the API as the `harborrag-api` job. This rule, added
to a Prometheus rule file such as `deploy/prometheus/harborrag_alerts.yml`, fires when
slots have been skipped every hour for two hours:

```yaml
groups:
  - name: harborrag-api-schedules
    rules:
      - alert: HarborRAGScheduleSlotsKeepSkipping
        expr: sum by (reason) (increase(harborrag_schedule_slots_total{outcome="skipped"}[1h])) > 0
        for: 2h
        labels:
          severity: warning
        annotations:
          summary: HarborRAG schedule slots keep being skipped
          description: "Schedule slots were skipped with reason {{ $labels.reason }} in every hour for two hours."
```

A schedule whose slots often meet a long run skips with `already_running` as intended;
leave that reason out of the rule, or set `if_running: queue` on the schedule.

## Declare schedules in a file

`config/sources/schedules.yaml` declares schedules that the API applies to
[Temporal Schedules](https://docs.temporal.io/schedule) each time it starts, so they can
live in your deployment's version control. `/v1/admin/schedules` does not list them, and
they run as described in [How a declared run works](#how-a-declared-run-works).

A declared schedule names its connection by `source_key`. A connection imported from
`config/sources/connectors.yaml` uses its YAML key, such as `harborrag-workspace`; a
connection created through the API or `harborrag connections create`, or one that
existed before database migration 0041, uses `PROJECT_ID/CONNECTION_ID`, such as
`default/harborrag-workspace`.

Use the Temporal UI to inspect declared runs and for emergency administration, but do
not edit a declared schedule there: the next sync overwrites the change. Pause state is
the exception. A sync pauses or unpauses an existing schedule only when its `paused`
value in the file changes, so a pause applied during an incident survives restarts until
someone unpauses it.

### Write the file

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
    `PROJECT_ID/CONNECTION_ID` address. Leave `source_scope_id: null` so scheduled runs
    use the same source scope as a run started with `POST /v1/admin/ingestions`; a
    different scope is ingested and tracked separately.

3. Restart the API, for example with `scripts/deployment/dev.sh down` and then
    `scripts/deployment/dev.sh up`. The API reads the file only at startup. The Compose
    stack mounts `config/` into the API container read-only, so no image rebuild is
    needed. The API reads the file from `HARBORRAG_SCHEDULE_CONFIG_PATH`, which defaults
    to `config/sources/schedules.yaml` relative to the working directory and is set to
    `/app/config/sources/schedules.yaml` in the API image and the Compose stack.

### Check that the file applied

The API log reports each sync:

```bash
docker logs harborrag-api-api-1 2>&1 | grep -i schedule
```

A successful first sync of one schedule logs
`Declared schedules synced created=1 updated=0 deleted=0 failed=0`; later restarts count
it under `updated`. The schedule appears under **Schedules** in the Temporal UI, at
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

### Schedule file reference

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

### How a declared run works

Each firing starts the `harborrag.scheduled_source_ingestion` workflow on the configured
discovery task queue, which starts the normal `harborrag.source_ingestion` workflow as a
child. A firing does not replay an earlier Temporal run or discover records that the
connection's query excludes.

- Each firing reads the connection from the control database, so a change to the
  connection applies from the next firing, with no restart. Changes to the worker's own
  configuration, such as its task queues, apply after the workers restart.
- When the connection was removed, is being deleted or holds invalid values, the firing
  fails at once and is not retried.
- When the connection already has a pending, running or paused ingestion, such as a
  manual ingestion or another schedule's run, the firing is skipped and recorded with
  outcome `skipped` instead of failing. A run keeps the connection busy until it
  finishes, which can be hours after its scan ends, and a paused run until it is
  resumed or cancelled. Repeated skips with no completed run mean the connection is
  held by a long-running, paused or stranded run; the `HarborRAGScheduleKeepsSkipping`
  alert reports this after a day.

### Monitor declared runs

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
schedule outside `/v1/admin/schedules` every
`HARBORRAG_SCHEDULE_METRICS_INTERVAL_SECONDS` (default 30) to export the pause and
overlap gauges; with several worker replicas, set it to `0` on all but one to avoid
duplicate Temporal load and series.

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

## Schedules created in 1.x

The 1.x `/v1/schedules` routes answer `404`, and `/v1/admin/schedules` does not list the
schedules created through them. Those schedules keep running, because a file sync
deletes only schedules the file created. Pause or delete them in the Temporal UI.
