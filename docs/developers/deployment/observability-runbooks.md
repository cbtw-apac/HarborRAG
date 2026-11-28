# Observability runbooks

Respond to each HarborRAG alert: what it means, where to look, how to fix it and how to
confirm the fix. Every alert carries a `side` label (`online`, `offline` or `shared`), a
`dashboard` annotation naming the Grafana dashboard to open, and a link to its section here.
Critical alerts go to the on-call webhook and warnings to the team webhook. Both are set in
`env/.env.monitoring`; see [Run the monitoring stack](README.md#run-the-monitoring-stack).

Every section assumes the monitoring stack is running and the services export telemetry, as
[Turn telemetry export on](README.md#turn-telemetry-export-on) describes. Thresholds shared by
alerts and dashboards are recording rules at the top of
[`deploy/prometheus/harborrag_alerts.yml`](../../../deploy/prometheus/harborrag_alerts.yml).

## Find the trace behind a red panel

Every investigation ends at a trace and its logs, using links only:

1. Open the dashboard the alert names. In its bottom band, open a row of the slowest and
   failed table, or click an exemplar dot on a latency panel.
2. In the trace, the span marked as an error is where the call failed. Store and retrieval
   spans (`retrieval.*`, `harborrag.ingestion.*`) show which backend took the time.
3. Open the trace's logs from the span. Loki shows the lines that carry the same `trace_id`.

Traces and logs hold IDs, counts and durations only. Query text, prompts, answers, document
text, titles and file names never reach them.

## MCP down

**Severity:** critical, online. The collector's probe of the MCP server's `/healthz` has not
answered 2xx for 2 minutes, or the probe stopped reporting while the collector is up.

1. Check the MCP container: `docker ps --filter name=harborrag-mcp`. A restarting or missing
   container is the outage.
2. Read its last logs: `scripts/deployment/mcp.sh logs`.
3. Restart it with `scripts/deployment/mcp.sh --http`. The command waits until the server is
   healthy.

**Verify:** the alert resolves within 2 minutes and the Online overview's MCP stats return.
**Escalate** when the container keeps restarting: the cause is in its startup log, often an
unreachable database.

## MCP errors

**Severity:** critical, online. More than 5% of MCP tool calls failed server-side for 10
minutes: a server error, or a tool failure with `error_code` `backend_unavailable` (a store
or model the tool could not reach) or `not_configured` (no backend for the tool). Calls that
failed for a caller or deployment reason do not count: `invalid_request`, `query_too_broad`,
`not_enabled` and `no_indexed_content`, and refused calls.

1. Open MCP and retrieval. *Failures by kind* confirms the failures are server-side, and the
   *Server-side* column of *Per-tool summary* shows which tool fails.
2. Open a failed call from *Slowest and failed calls, last hour*. The failed span names the
   cause's type, its `harborrag.error.code` and the store span that raised it.
3. If one store fails for every tool, open Stores and host and check that store's container.

**Verify:** *Error rate, 5 min* drops below 5% and the alert resolves.

## MCP slow

**Severity:** warning, online. The p95 of MCP tool calls stayed above
`harborrag:threshold:mcp_p95_seconds` (2 seconds until a baseline exists) for 15 minutes.

1. Open MCP and retrieval. The *p95* column of *Per-tool summary* names the slow tool.
2. *Mean time per retrieval step* shows whether dense embedding, the vector query, graph
   expansion or evidence loading grew.
3. Open Online overview's *Host memory and CPU*. A shaded ingestion run under a CPU peak means
   a sync is starving MCP on the shared host.

**Verify:** *p95 latency, 5 min* stays under the threshold. If the threshold is wrong rather
than the service, change the recording rule, not the alert.

## Empty results spike

**Severity:** warning, online. In the last hour, successful retrieval calls returned no result
at least twice as often as over the last 7 days, and more than 10% of the time. Retrieval calls
are `vector_search` and the graph tools; a subgraph with no relations counts as empty.

1. Open MCP and retrieval. *Empty-result rate by tool* shows which tool returns nothing.
2. Check Ingestion and schedules for a failed or partial run: a source that lost its index
   returns nothing for its queries. If the empty tool is a graph tool, MCP and retrieval's
   *Stale or unpublished candidates dropped* shows whether the graph projection lags behind.
3. A spike after a deploy points at the deploy; the dashboards mark each deploy.

**Verify:** *Empty-result rate, 1 h* returns to its usual level.

## LLM spend

**Severity:** warning, online. The model cost all services reported over the last 24 hours is
above `harborrag:threshold:llm_daily_budget_usd` (50 USD). Online overview's *Spend vs budget*
shows how close the spend is before the alert fires.

1. Open Chat and LLM. *Cost per day by service and model* shows whether ingestion
   (`harborrag-worker`), chat (`harborrag-api`) or search (`harborrag-mcp`) spends, and on which
   model.
2. If ingestion spends, open Ingestion and schedules: *Model cost per interval* lines the spend
   up with the runs. A forced re-sync embeds every document again; *Cost per 1k docs*
   gives the unit price.
3. If chat spends, *Tokens per day by service and model* shows whether input tokens grew, which
   means longer contexts per turn.

The cost is what the LiteLLM proxy reports from its price list, an estimate rather than the
invoice, and self-hosted models report 0.

**Verify:** the 24-hour cost falls under the budget. To change the budget, edit the recording
rule.

## Host resources

**Severity:** critical, shared. Host memory is above 90%, or a real filesystem is more than
85% full, for 5 minutes. The `resource` label says which.

1. Open Stores and host. *Memory per container* names the container that grew.
2. For disk, check the monitoring stack's own volumes as well: Prometheus keeps 30 days,
   Loki 14 and Tempo 7.

**Verify:** the gauges in *Is it healthy now* fall under the alert levels.
**Escalate** when the host keeps running out: move the monitoring stack to a separate host.

## Source freshness unknown

**Severity:** warning, shared. For 10 minutes, no API replica has refreshed the source gauges
in the last 5 minutes, or none reports them at all while the API is up. The API computes
these gauges from the run history in PostgreSQL. While they are stale, Ingestion and
schedules' source panels show old values and the *Source run failed* and *Source run not
started* alerts cannot fire.

1. In Prometheus, check that the `harborrag-api` target is up. A down target or a failed
   scrape is covered in [If the API scrape fails](README.md#if-the-api-scrape-fails).
2. Open Stores and host and check PostgreSQL: the refresh reads the control database.
3. Find the failed refreshes in Grafana's Explore, on Loki, with
   `{service_name="harborrag-api"} | event="observability.source_metrics_refresh_failed"`.
   Each line names the error type; `error_code="timeout"` means the read of the run history
   timed out.

**Verify:** `time() - max(harborrag_ingestion_source_state_refresh_timestamp_seconds)` stays
under 300 in Prometheus and the alert resolves.

## Index partial failures

**Severity:** warning, offline. Failed vector or graph write attempts exceeded 1% of the
documents admitted in the last hour. A document whose other index succeeded stays findable,
so this is a warning, not a failed document.

1. Open Ingestion and schedules. *Partial index failures* shows whether `qdrant_write`
   (vector) or `falkordb_write` (graph) fails.
2. Open the *Failed documents* logs and follow a line to its trace.
3. Check the failing store in Stores and host.

**Verify:** no new failed write attempts appear after the store recovers.

## Sync missed or stale

**Severity:** warning, offline. A schedule that is not paused has completed no run in 26
hours.

1. Open Ingestion and schedules. *Source runs* shows whether runs failed, kept running or
   never started.
2. A run that never started points at the worker: check that it runs with
   `docker ps --filter name=temporal-worker`, then check *Temporal pickup delay per task queue*.
3. Runs that fail point at the connector: check *Connector errors and 429s*.

**Verify:** the schedule completes a run and the alert resolves.

## Source run failed

**Severity:** warning, offline. A source's latest run failed, and no newer run has replaced it
for 10 minutes. The alert names the tenant, source scope and connector type. It covers every
run, scheduled or started by hand, and stays until the source's next run.

1. Open Ingestion and schedules. *Last sync per source and tenant* shows how long the source
   has gone without a completed run, and *Source runs* shows whether its earlier runs failed
   too.
2. In the *Failed documents* logs, the run's `ingestion.source_failed` line names the error
   type and the stage that failed. Open its trace from the line.
3. A connector failure shows in *Connector errors and 429s*; a store failure in Stores and
   host.

**Verify:** the source's next run completes and the alert resolves.

## Source run not started

**Severity:** warning, offline. A source's latest run has been pending for 30 minutes: it was
submitted, but no worker picked it up.

1. Check that the worker runs: `docker ps --filter name=temporal-worker`.
2. Open Ingestion and schedules. *Temporal pickup delay per task queue* and *Activity slots in
   use per task queue* show whether the worker is busy with other runs; if so, follow
   [Temporal backlog](#temporal-backlog).
3. Open the *Temporal: workflows* link and find the source's workflow to see whether it was
   scheduled at all.

**Verify:** *Source runs* shows the source running, and the alert resolves.

## Temporal backlog

**Severity:** warning, offline. On one task queue, the p95 time activities waited for a free
worker slot stayed above `harborrag:threshold:temporal_pickup_p95_seconds` (120 seconds) for 30
minutes. The worker records this wait, so a stopped worker never triggers the alert; Sync missed
or stale catches that case later.

1. Open Ingestion and schedules. *Temporal pickup delay per task queue* names the queue.
2. *Activity slots in use per task queue* shows whether its slots stay at the dashed line,
   `worker.max_concurrent_activities` in `config/temporal.yaml`.
3. If the slots are full and Online overview's *Host memory and CPU* shows free CPU, give the
   worker more capacity: raise `HARBORRAG_TEMPORAL_WORKER_REPLICAS` in `env/.env.temporal`, or
   raise `worker.max_concurrent_activities` and rebuild the worker, which bakes that file in,
   with `scripts/deployment/dev.sh worker --build`. If the CPU is busy too, the host is the
   limit: spread the schedules out instead.

**Verify:** the pickup delay falls back to seconds and the alert resolves.
