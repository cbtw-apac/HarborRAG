# HarborRAG Grafana dashboards

Put exported Grafana dashboard JSON files in this folder. The monitoring Compose file mounts
it read-only at `/var/lib/grafana/dashboards`, and
`../provisioning/dashboards/dashboards.yml` loads every dashboard there into the `HarborRAG`
folder in Grafana. No dashboard ships yet. To start the monitoring stack, see
[Run the monitoring stack](../../../docs/developers/deployment/README.md#run-the-monitoring-stack).
