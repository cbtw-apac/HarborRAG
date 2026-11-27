-- Creates or refreshes harborrag_observer, the read-only Postgres role of the
-- monitoring stack: Grafana's PostgreSQL data source and postgres-exporter log in as it.
-- Run it with psql, as the role that runs the migrations, on the control database;
-- `scripts/deployment/observability.sh db-access` does that with
-- HARBORRAG_OBSERVER_PASSWORD from the monitoring environment file.
-- pg_monitor gives the server and database statistics. On the control tables the role
-- reads only the run columns Grafana charts: ingestion_tasks.request (source paths and
-- filters), ingestion_tasks.idempotency_key and task_document_results.result (document
-- titles, file names and source item ids) stay unreadable. A table the migrations have
-- not created yet is skipped; run this again after they have.
\set ON_ERROR_STOP on
BEGIN;
SET LOCAL log_statement = 'none';
SET LOCAL log_min_error_statement = panic;
SET LOCAL client_min_messages = warning;

SELECT 'CREATE ROLE harborrag_observer NOLOGIN'
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'harborrag_observer')
\gexec
ALTER ROLE harborrag_observer NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
ALTER ROLE harborrag_observer SET default_transaction_read_only = on;
GRANT pg_monitor TO harborrag_observer;

\getenv observer_password HARBORRAG_OBSERVER_PASSWORD
\if :{?observer_password}
ALTER ROLE harborrag_observer LOGIN PASSWORD :'observer_password';
\endif

SELECT format('GRANT CONNECT ON DATABASE %I TO harborrag_observer', current_database())
\gexec
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO harborrag_observer;

-- Table-level REVOKE also removes column grants, so a column dropped from the lists
-- below loses its grant on the next run.
SELECT format('REVOKE ALL ON TABLE public.%I FROM harborrag_observer', name)
FROM unnest(ARRAY['ingestion_tasks', 'task_document_results']) AS name
WHERE to_regclass(format('public.%I', name)) IS NOT NULL
\gexec

SELECT format('GRANT SELECT (%s) ON TABLE public.%I TO harborrag_observer', columns, name)
FROM (VALUES
    (
        'ingestion_tasks',
        'task_id, tenant_id, source_scope_id, status, submitted_at, started_at, '
        'completed_at, summary, event_sequence'
    ),
    (
        'task_document_results',
        'task_id, document_id, document_version_id, status, completed_at'
    )
) AS grants (name, columns)
WHERE to_regclass(format('public.%I', name)) IS NOT NULL
\gexec
COMMIT;
