"""One success result per CLI-mapped operation, as the operations port returns it.

Each body is a response-model instance with DEMO data, wrapped the way the port wraps it
(``Tagged``, ``Saved``, ``Accepted``, ``ChatCompletion``), so ``FakeOperations(results=SAMPLES)``
answers every mapped command, including the ETag read an update makes first.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime

from harborrag_app.api.v1.admin.connection_schemas import (
    CheckOut,
    ConnectionOut,
    ConnectionPage,
    ConnectionStatsOut,
    ConnectionTestOut,
    CountersOut,
    IngestionOut,
    JobAcceptedOut,
    LastIngestionOut,
    LinksOut,
    PreviewOut,
    ProjectOut,
    ProjectPage,
    SampleItemOut,
    SecretStatusOut,
    SourceTypeOut,
    SourceTypePage,
    TriggeredByOut,
)
from harborrag_app.api.v1.admin.settings_schemas import (
    SettingChangeOut,
    SettingsOut,
    SettingsPatchOut,
)
from harborrag_app.api.v1.chat.schemas import ChatCompletionResponse, Citation, Usage
from harborrag_app.api.v1.ingestion.schemas import (
    FailureOut,
    FailurePage,
    FailureRetryAccepted,
    IngestionPage,
)
from harborrag_app.api.v1.me_schemas import MeOut, MePrincipal
from harborrag_app.operations import Accepted, Saved, Tagged
from harborrag_app.operations.port import ChatCompletion

_STAMP = datetime(2026, 11, 10, tzinfo=UTC)
_ETAG = 'W/"rev-3"'
_REF = "engineering/jira-main"

_PROJECT = ProjectOut(
    project_id="engineering",
    tenant_id="DEFAULT",
    project_name="Engineering",
    project_description="Engineering tickets and design notes",
    connection_count=1,
    document_count=42,
    created_at=_STAMP,
    updated_at=_STAMP,
)

_SOURCE_TYPE = SourceTypeOut.model_validate(
    {
        "source_type": "jira",
        "name": "Jira",
        "category": "issue_tracker",
        "icon": "jira",
        "version": "2.0.0",
        "docs_url": "https://example.com/docs/jira",
        "auth_methods": [
            {
                "auth_method_id": "api_token",
                "title": "API token",
                "schema": {
                    "type": "object",
                    "properties": {"api_token": {"type": "string", "writeOnly": True}},
                },
                "ui": {
                    "api_token": {
                        "group": "credentials",
                        "order": 1,
                        "widget": "secret",
                        "help": "An Atlassian API token.",
                        "placeholder": "",
                    }
                },
            }
        ],
        "config_schema": {"type": "object", "properties": {"jql": {"type": "string"}}},
        "advanced_schema": {"type": "object", "properties": {"page_size": {"type": "integer"}}},
        "ui": {
            "jql": {
                "group": "config",
                "order": 1,
                "widget": "text",
                "help": "Which issues to sync.",
                "placeholder": "project = DEMO",
            }
        },
        "capabilities": {"incremental": True, "acl_sync": False, "attachments": True},
    }
)

_CONNECTION = ConnectionOut(
    tenant_id="DEFAULT",
    project_id="engineering",
    connection_id="jira-main",
    source_type="jira",
    connection_ref=_REF,
    name="Jira main",
    credentials={
        "method": "api_token",
        "base_url": "https://example.atlassian.net",
        "api_token": SecretStatusOut(set=True, last4="a1b2", updated_at=_STAMP, from_="stored"),
    },
    config={"jql": "project = DEMO"},
    advanced={"page_size": 50},
    status="active",
    last_ingestion=LastIngestionOut(ingestion_id="ing_1", status="succeeded", finished_at=_STAMP),
    stats=ConnectionStatsOut(documents=42, chunks=310, entities=None, errors=0),
    created_by="cli:alex",
    updated_by="cli:alex",
    created_at=_STAMP,
    updated_at=_STAMP,
)

_CONNECTION_TEST = ConnectionTestOut(
    ok=True,
    checks=[CheckOut(name="auth", ok=True, message=None)],
    error_category=None,
    preview=PreviewOut(
        estimated_items=42,
        sample=[SampleItemOut(item_ref="DEMO-1", title="Roll out the DEMO dashboard")],
    ),
    latency_ms=120,
)

_INGESTION = IngestionOut(
    ingestion_id="ing_1",
    tenant_id="DEFAULT",
    project_id="engineering",
    connection_id="jira-main",
    mode="incremental",
    status="running",
    counters=CountersOut(discovered=4, processed=2, skipped=0, failed=1),
    stages=[{"stage": "capture", "status": "succeeded"}],
    error_category=None,
    triggered_by=TriggeredByOut(principal_type="user", principal_id="cli:alex"),
    settings_revision="rev_3",
    workflow_id="harborrag-source:ing_1",
    run_id="ing_1",
    trace_id="0" * 32,
    links=LinksOut(temporal="", grafana="", langfuse=""),
    created_at=_STAMP,
    finished_at=None,
)

_FAILURE = FailureOut(
    failure_id="fail_1",
    ingestion_id="ing_1",
    connection_ref=_REF,
    document_ref="DEMO-1",
    stage="interpret",
    error_category="parse_error",
    message="the parser gave up",
    attempts=1,
    retryable=True,
    resolved=False,
    created_at=_STAMP,
)

_ANSWER = ChatCompletionResponse(
    completion_id="msg_1",
    session_id=None,
    trace_id="1" * 32,
    mode="classic",
    answer="Alex Example owns the DEMO-1 rollout [1].",
    citations=[
        Citation(
            n=1,
            chunk_id="chunk_1",
            document_id="doc_1",
            title="DEMO-1 rollout plan",
            snippet="Alex Example owns the rollout.",
            url="https://example.atlassian.net/browse/DEMO-1",
            page=None,
        )
    ],
    usage=Usage(
        mode="classic",
        reasoning="off",
        steps=1,
        prompt_tokens=10,
        completion_tokens=5,
        reasoning_tokens=0,
        cost_usd=None,
    ),
)

SAMPLES: Mapping[str, object] = {
    "me": MeOut(
        principal=MePrincipal(principal_type="user", principal_id="cli:alex", name="cli:alex"),
        tenant_id="DEFAULT",
        tenants=["*"],
        role="owner",
    ),
    "list_projects": ProjectPage(items=[_PROJECT], next_cursor=None),
    "get_project": Tagged(_PROJECT, _ETAG),
    "put_project": Saved(_PROJECT, _ETAG, created=True),
    "delete_project": JobAcceptedOut(job_id="job_1"),
    "list_source_types": SourceTypePage(items=[_SOURCE_TYPE]),
    "list_connections": ConnectionPage(items=[_CONNECTION], next_cursor=None),
    "get_connection": Tagged(_CONNECTION, _ETAG),
    "put_connection": Saved(_CONNECTION, _ETAG, created=True),
    "delete_connection": JobAcceptedOut(job_id="job_2"),
    "test_connection": _CONNECTION_TEST,
    "start_ingestion": Accepted(_INGESTION, "/v1/admin/ingestions/ing_1"),
    "list_ingestions": IngestionPage(items=[_INGESTION], next_cursor=None),
    "get_ingestion": _INGESTION,
    "pause_ingestion": _INGESTION.model_copy(update={"status": "paused"}),
    "resume_ingestion": _INGESTION,
    "cancel_ingestion": _INGESTION.model_copy(update={"status": "cancelled"}),
    "retry_ingestion_failures": Accepted(
        _INGESTION.model_copy(update={"ingestion_id": "ing_2"}), "/v1/admin/ingestions/ing_2"
    ),
    "list_failures": FailurePage(items=[_FAILURE], next_cursor=None, counts={"interpret": 1}),
    "retry_failures": FailureRetryAccepted(ingestion_id="ing_2", ingestion_ids=["ing_2"], queued=1),
    "settings_schema": {
        "chunking": {
            "chunking.max_tokens": {
                "type": "integer",
                "minimum": 64,
                "levels": ["tenant", "project", "connection"],
                "applies": "next_ingestion",
            }
        }
    },
    "read_settings": Tagged(
        SettingsOut(level="tenant", revision="rev_3", values={"chunking.max_tokens": 512}), _ETAG
    ),
    "patch_settings": Tagged(
        SettingsPatchOut(
            revision="rev_4",
            dry_run=False,
            changed=[
                SettingChangeOut(
                    key="chunking.max_tokens", **{"from": 512}, to=256, applies="next_ingestion"
                )
            ],
        ),
        'W/"rev-4"',
    ),
    "create_chat_completion": ChatCompletion(_ANSWER, None, replayed=False),
}
