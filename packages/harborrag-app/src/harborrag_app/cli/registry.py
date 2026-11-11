"""Which operation each CLI command calls, and what has no counterpart on the other side.

Owns the classification the parity check holds the CLI and HTTP to: every operationId of the
OpenAPI export is a command's operation or HTTP-only, and every command maps to an operation,
is CLI-only or is a hidden legacy stub (``commands.legacy.LEGACY``). Data only; each command
still names its operation when it calls ``run_operation``.

Start from: COMMAND_OPERATIONS, HTTP_ONLY, CLI_ONLY.
"""

from __future__ import annotations

from collections.abc import Mapping

COMMAND_OPERATIONS: Mapping[str, str] = {
    "me": "me",
    "projects list": "list_projects",
    "projects get": "get_project",
    "projects create": "put_project",
    "projects update": "put_project",
    "projects delete": "delete_project",
    "source-types list": "list_source_types",
    "connections list": "list_connections",
    "connections get": "get_connection",
    "connections create": "put_connection",
    "connections update": "put_connection",
    "connections delete": "delete_connection",
    "connections test": "test_connection",
    "ingestions start": "start_ingestion",
    "ingestions list": "list_ingestions",
    "ingestions get": "get_ingestion",
    "ingestions pause": "pause_ingestion",
    "ingestions resume": "resume_ingestion",
    "ingestions cancel": "cancel_ingestion",
    "ingestions retry-failures": "retry_ingestion_failures",
    "failures list": "list_failures",
    "failures retry": "retry_failures",
    "settings schema": "settings_schema",
    "settings get": "read_settings",
    "settings set": "patch_settings",
    "chat": "create_chat_completion",
}
"""The operationId each command calls, keyed by its path after ``harborrag``."""

HTTP_ONLY: Mapping[str, str] = {
    "create_chat_session": "Chat sessions belong to the web chat; chat --session-id continues one",
    "list_chat_sessions": "Browsing chat history is a web console view",
    "list_conversation_messages": "Reading back a session's messages is a web console view",
    "list_searchable_connections": "Feeds the chat's connection picker for readers",
    "list_ingestion_documents": "Per-document drill-down of a run is a web console view",
    "stream_ingestion": "Server-sent events for the web console; ingestions get --watch polls",
    "healthz": "Liveness probe for the orchestrator",
    "readyz": "Readiness probe for the orchestrator; doctor runs the same checks locally",
}
"""Operations with no command, by operationId, with the reason."""

CLI_ONLY: Mapping[str, str] = {
    "doctor": "Checks the local project, configuration and services",
    "config import": "Writes a settings file as one revision; no HTTP route in v2.0.0",
    "config export": "Prints the stored overrides as a settings file; no HTTP route in v2.0.0",
    "auth keys create": "MCP reader keys are managed from the CLI until v2.1.0",
    "auth keys list": "MCP reader keys are managed from the CLI until v2.1.0",
    "auth keys revoke": "MCP reader keys are managed from the CLI until v2.1.0",
}
"""Commands with no operation, by path after ``harborrag``, with the reason."""
