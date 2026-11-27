#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
# shellcheck source=common/dev-checks.sh
source "${ROOT_DIR}/scripts/deployment/common/dev-checks.sh"

DATABASE_ENV_FILE="${DATABASE_ENV_FILE:-env/.env.database}"
MONITORING_ENV_FILE="${MONITORING_ENV_FILE:-env/.env.monitoring}"
MONITORING_TEMPLATE="env-example/.env.monitoring.example"
MONITORING_STARTUP_TIMEOUT=120

usage() {
    cat <<'EOF'
Usage: scripts/deployment/observability.sh COMMAND [OPTION...]

Runs the optional monitoring stack (deploy/compose/docker-compose.monitoring.yml):
an OpenTelemetry collector, Tempo, Loki, Prometheus, Grafana, and host, container
and PostgreSQL exporters. No other script starts it.

Commands:
  bootstrap          Create the protected monitoring env file with generated
                     passwords, then stop for review
  up                 Start the stack in the background; the data services must
                     be running ('scripts/deployment/dev.sh data')
  down [--volumes]   Stop the stack; --volumes also deletes the stored metrics,
                     logs, traces and Grafana state
  status             List the stack's containers
  logs [SERVICE...]  Show the logs of the stack, or of the named services
  db-access          Create or refresh the read-only harborrag_observer
                     PostgreSQL role that Grafana and postgres-exporter use; run
                     it after 'dev.sh up' and after every upgrade

The API, worker and MCP server export telemetry only when their env file sets
OTEL_EXPORTER_OTLP_ENDPOINT; 'up' prints the line to add.

Environment file paths can be overridden with MONITORING_ENV_FILE and
DATABASE_ENV_FILE. Only POSTGRES_DB is read from the database file.
EOF
}

require_database_environment() {
    [[ -f "${ROOT_DIR}/${DATABASE_ENV_FILE}" ]] ||
        fail "Missing database environment: ${DATABASE_ENV_FILE}. Run 'scripts/deployment/dev.sh bootstrap'."
}

# The stack's Compose command. It sees the monitoring env file and, from the database
# one, only POSTGRES_DB: never the PostgreSQL owner or MinIO root credentials.
monitoring_compose() {
    require_file "${MONITORING_ENV_FILE}" "monitoring environment"
    require_database_environment
    POSTGRES_DB="$(env_file_value "${DATABASE_ENV_FILE}" POSTGRES_DB)"
    export POSTGRES_DB
    # Compose refuses a secret whose variable is unset, and env files written before
    # alerting have no webhook lines: a missing line counts as a blank URL.
    local name
    for name in HARBORRAG_ALERT_ONCALL_WEBHOOK_URL HARBORRAG_ALERT_TEAM_WEBHOOK_URL; do
        export "${name}=$(env_file_value "${MONITORING_ENV_FILE}" "${name}")"
    done
    docker compose \
        --env-file "${ROOT_DIR}/${MONITORING_ENV_FILE}" \
        --file "${ROOT_DIR}/deploy/compose/docker-compose.monitoring.yml" \
        "$@"
}

data_compose() {
    require_database_environment
    docker compose \
        --env-file "${ROOT_DIR}/${DATABASE_ENV_FILE}" \
        --file "${ROOT_DIR}/deploy/compose/docker-compose.database.yml" \
        "$@"
}

bootstrap_environment() {
    created_environment=0
    ensure_environment_file "${MONITORING_ENV_FILE}" "${MONITORING_TEMPLATE}"
    ensure_generated_value "${MONITORING_ENV_FILE}" GRAFANA_ADMIN_PASSWORD
    ensure_generated_value "${MONITORING_ENV_FILE}" HARBORRAG_OBSERVER_PASSWORD
}

print_export_steps() {
    local grafana_port collector_port
    grafana_port="$(env_file_value "${MONITORING_ENV_FILE}" GRAFANA_PORT)"
    collector_port="$(env_file_value "${MONITORING_ENV_FILE}" OTEL_COLLECTOR_PORT)"
    echo "Grafana: http://127.0.0.1:${grafana_port:-3030}/ (GRAFANA_ADMIN_USER and GRAFANA_ADMIN_PASSWORD in ${MONITORING_ENV_FILE})"
    echo
    echo "To export traces, metrics and logs, add this line to env/.env.api, env/.env.temporal and env/.env.mcp:"
    echo "  OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4318"
    echo "then restart the services that read them:"
    echo "  scripts/deployment/dev.sh api"
    echo "  scripts/deployment/dev.sh worker"
    echo "  scripts/deployment/mcp.sh --http"
    echo "A process run from the checkout exports to http://127.0.0.1:${collector_port:-4318} instead."
    echo
    echo "Grafana's PostgreSQL data source and postgres-exporter log in as harborrag_observer; create it with '$0 db-access'."
}

start_stack() {
    require_data_network
    # An env file from before the observer role has no password for it; Grafana and
    # postgres-exporter refuse to start without one.
    require_file "${MONITORING_ENV_FILE}" "monitoring environment"
    ensure_generated_value "${MONITORING_ENV_FILE}" HARBORRAG_OBSERVER_PASSWORD
    monitoring_compose config --quiet
    echo "Starting the HarborRAG monitoring stack..."
    monitoring_compose up --detach --wait --wait-timeout "${MONITORING_STARTUP_TIMEOUT}"
    print_export_steps
}

stop_stack() {
    local -a down_args=(down)
    if [[ "${1:-}" == "--volumes" ]]; then
        down_args+=(--volumes)
        shift
    fi
    [[ "$#" -eq 0 ]] || fail "Unknown down option: $1"
    echo "Stopping the HarborRAG monitoring stack..."
    monitoring_compose "${down_args[@]}"
}

# Create or refresh harborrag_observer with the password in the monitoring env file,
# generating a blank one first. The password reaches psql through the environment,
# never the command line. Grants apply to the tables that exist now, so this runs
# after the migrations, as the Postgres owner.
grant_observer_access() {
    require_file "${MONITORING_ENV_FILE}" "monitoring environment"
    ensure_generated_value "${MONITORING_ENV_FILE}" HARBORRAG_OBSERVER_PASSWORD
    require_data_network

    echo "Granting the monitoring stack read-only access to PostgreSQL..."
    # shellcheck disable=SC2016  # expanded by the shell inside the container
    HARBORRAG_OBSERVER_PASSWORD="$(env_file_value "${MONITORING_ENV_FILE}" HARBORRAG_OBSERVER_PASSWORD)" \
        data_compose exec -T -e HARBORRAG_OBSERVER_PASSWORD postgres \
        sh -c 'psql -q -v ON_ERROR_STOP=1 -U "${POSTGRES_USER}" -d "${POSTGRES_DB}"' \
        <"${ROOT_DIR}/deploy/compose/postgres/observer-role.sql"
    echo "Grafana and postgres-exporter read PostgreSQL as harborrag_observer."
    echo "After a password change, run '$0 up' so they use the new one."
}

command="${1:-}"
[[ -n "${command}" ]] || {
    usage
    exit 2
}
shift

case "${command}" in
    bootstrap)
        [[ "$#" -eq 0 ]] || fail "bootstrap accepts no options."
        bootstrap_environment
        if ((created_environment)); then
            echo "Review ${MONITORING_ENV_FILE}: copy HARBORRAG_METRICS_SCRAPE_TOKEN from env/.env.api into it, then run '$0 up'."
        else
            echo "${MONITORING_ENV_FILE} already exists."
        fi
        ;;
    up)
        [[ "$#" -eq 0 ]] || fail "up accepts no options."
        start_stack
        ;;
    down)
        stop_stack "$@"
        ;;
    status)
        [[ "$#" -eq 0 ]] || fail "status accepts no options."
        monitoring_compose ps
        ;;
    logs)
        monitoring_compose logs "$@"
        ;;
    db-access)
        [[ "$#" -eq 0 ]] || fail "db-access accepts no options."
        grant_observer_access
        ;;
    -h|--help|help)
        usage
        ;;
    *)
        usage >&2
        fail "Unknown command: ${command}"
        ;;
esac
