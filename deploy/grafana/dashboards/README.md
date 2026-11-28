# HarborRAG Grafana dashboards

Put exported Grafana dashboard JSON files in one of the three subfolders. The monitoring
Compose file mounts this folder read-only at `/var/lib/grafana/dashboards`, and
`../provisioning/dashboards/dashboards.yml` loads each subfolder into its own Grafana folder:

| Subfolder | Grafana folder | Folder uid |
| --- | --- | --- |
| `online/` | `Online` | `harborrag-online` |
| `offline/` | `Offline` | `harborrag-offline` |
| `shared/` | `Shared` | `harborrag-shared` |

A JSON file placed directly in this folder is not loaded. Panels refer to the data sources by
their fixed uids: `harborrag-prometheus`, `harborrag-loki` and `harborrag-tempo`.

| File | Dashboard | Uid |
| --- | --- | --- |
| `online/online-overview.json` | Online overview | `harborrag-online-overview` |
| `online/mcp-retrieval.json` | MCP and retrieval | `harborrag-mcp-retrieval` |
| `online/chat-llm.json` | Chat and LLM | `harborrag-chat-llm` |
| `offline/ingestion-schedules.json` | Ingestion and schedules | `harborrag-ingestion` |
| `shared/stores-host.json` | Stores and host | `harborrag-stores-host` |

The admin console links a run to `harborrag-ingestion` with `var-run=<task id>`, which narrows
*Documents by outcome per run* to that task and fills the *Temporal: this run* link. Any
dashboard also takes `var-tenant`, and the ingestion one `var-connector` and `var-task_queue`,
in its URL.

The ingestion dashboard reads no SQL. Its source panels read the API's
`harborrag_ingestion_source_last_success_timestamp_seconds` and
`harborrag_ingestion_source_last_run_status` gauges, and *Documents by outcome per run* reads
the worker's `ingestion.source_finalized` log lines from Loki. Every API replica reports the
same source gauges, so each query aggregates them with `max by`. *Source runs* and the *Last
status* column turn the status label into a number, 1 (completed) to 7 (pending), and map the
number back to a named, coloured state; change both together. The dashboards' *Ingestion runs*
annotation shades the time a source's latest run was running.

The ingestion dashboard's Temporal panels compare the worker's activity slots with
`worker.max_concurrent_activities` in `config/temporal.yaml`. Its *Temporal: this run* link
opens the run's source workflow in the Temporal UI. The UI address and namespace are the
hidden constants `temporal_ui` (default `http://127.0.0.1:8080`) and `temporal_namespace`
(default `harborrag`); change them under Dashboard settings, Variables, when Temporal runs
elsewhere.

Series colours are fixed per entity, so a filter never repaints the series that remain: tool
families, graph tools, retrieval steps and Temporal task queues each keep one colour, taken in
order from a palette validated for colour-blind readers on Grafana's dark and light themes.
Failure kinds, the budget meter and thresholds use the separate status colours (good, warning,
serious, critical) and always carry a label. Every multi-series panel keeps its legend, because
two palette colours sit below 3:1 contrast on the light theme. A chart has one y-axis; two
measures of different scale get two panels. Keep these rules when you edit a panel.

Every dashboard reads top-down in four bands: is it healthy now, how is it trending, where is
it coming from, and what to open. Alerts, the admin console and the dashboards link to each
other by these uids, so keep them when you change a dashboard. Edit it in Grafana, export the
JSON without external data source references, and replace the file.

To start the monitoring stack, see
[Run the monitoring stack](../../../docs/developers/deployment/README.md#run-the-monitoring-stack).
