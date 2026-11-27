-- Creates or refreshes harborrag_mcp, the read-only Postgres role of the MCP server.
-- Run it with psql, as the role that runs the migrations, on the control database.
-- It grants SELECT on every table, now and later, then revokes it on the control-plane
-- tables the server never reads: secrets, connections, people, conversations, jobs.
-- The server reads the corpus tables, mcp_api_keys and the setting_* tables only.
-- With POSTGRES_MCP_PASSWORD in its environment it also sets the login password; the
-- compose Postgres runs it on an empty volume without one, so the role cannot log in
-- until `scripts/deployment/dev.sh mcp-access` runs it again.
\set ON_ERROR_STOP on
BEGIN;
SET LOCAL log_statement = 'none';
SET LOCAL log_min_error_statement = panic;

SELECT 'CREATE ROLE harborrag_mcp NOLOGIN'
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'harborrag_mcp')
\gexec
ALTER ROLE harborrag_mcp NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
ALTER ROLE harborrag_mcp SET default_transaction_read_only = on;

\getenv mcp_password POSTGRES_MCP_PASSWORD
\if :{?mcp_password}
ALTER ROLE harborrag_mcp LOGIN PASSWORD :'mcp_password';
\endif

SELECT format('GRANT CONNECT ON DATABASE %I TO harborrag_mcp', current_database())
\gexec
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO harborrag_mcp;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO harborrag_mcp;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO harborrag_mcp;

SELECT format('REVOKE ALL ON TABLE public.%I FROM harborrag_mcp', name)
FROM unnest(ARRAY[
    'activity',
    'alembic_version',
    'completion_requests',
    'connection_revisions',
    'conversation_memory_legacy',
    'conversation_messages',
    'conversation_sessions',
    'graph_conflicts',
    'ingestion_failures',
    'job_events',
    'jobs',
    'mcp_query_log',
    'members',
    'memories',
    'model_usage',
    'pending_control_plane_effects',
    'projects',
    'providers',
    'routing_rules',
    'schedule_slots',
    'schedules',
    'secrets',
    'singleton_leases',
    'sources',
    'workspace_settings'
]) AS name
WHERE to_regclass(format('public.%I', name)) IS NOT NULL
\gexec
COMMIT;
