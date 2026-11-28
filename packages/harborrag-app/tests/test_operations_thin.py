"""Every operation is a role check, one application-service call and the mapping of its result."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast

import pytest
from cli_operation_samples import SAMPLES

from harborrag_app.operations import ConnectionRef, LocalOperations, Operations, Scope
from harborrag_app.operations.contract import OPERATIONS
from harborrag_app.operations.etags import Preconditions
from harborrag_app.operations.principal import Principal
from harborrag_app.operations.results import Tagged
from harborrag_app.operations.schemas.chat import (
    ChatCompletionRequest,
    ChatCompletionResponse,
    ChatSessionCreateRequest,
)
from harborrag_app.operations.schemas.connections import (
    ConnectionWriteRequest,
    IngestionStartRequest,
    ProjectWriteRequest,
)
from harborrag_app.operations.schemas.ingestions import (
    FailureQuery,
    FailureRetryRequest,
    IngestionDocumentQuery,
    IngestionListQuery,
)
from harborrag_app.operations.schemas.schedules import ScheduleListQuery, ScheduleWriteRequest
from harborrag_core.domain.member import Role
from harborrag_core.domain.schedule import Schedule
from harborrag_runtime.application.chat.completion_request import CompletionOutcome
from harborrag_runtime.application.control_plane.schedule_views import schedule_view
from harborrag_runtime.application.control_plane.settings_service import (
    SettingsPatched,
    SettingsRead,
)
from harborrag_runtime.application.schemas import AppResponse

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

_SCOPE = Scope(Principal("u_demo", "owner", frozenset({"*"})), "acme")
_REF = ConnectionRef("engineering", "jira-main")
_NOW = datetime(2026, 11, 10, tzinfo=UTC)

NO_SERVICE_CALL = {
    "me": "describes the verified principal and reads no service",
    "list_source_types": "reads the static source-type registry, which is not an application service",
}
"""Operations that make no application-service call, with the reason."""


class _Unscripted(Exception):
    """The operation made a call the case did not expect."""


class _Recorder:
    """Application-service stand-in: every leaf call at any depth is recorded and answered."""

    def __init__(self, answers: Mapping[str, object]) -> None:
        self.calls: list[str] = []
        self._answers = answers

    def __getattr__(self, name: str) -> _Node:
        return _Node(self, name)


class _Node:
    def __init__(self, recorder: _Recorder, path: str) -> None:
        self._recorder = recorder
        self._path = path

    def __getattr__(self, name: str) -> _Node:
        return _Node(self._recorder, f"{self._path}.{name}")

    def __call__(self, *args: object, **kwargs: object) -> Any:
        self._recorder.calls.append(self._path)
        if self._path not in self._recorder._answers:
            raise _Unscripted(self._path)
        answer = self._recorder._answers[self._path]
        if self._path in _SYNC_CALLS:
            return answer
        return _answered(answer)


_SYNC_CALLS = frozenset({"settings_service.schema"})


async def _answered(answer: object) -> object:
    return answer


def _wire(sample: str) -> dict[str, Any]:
    """The response model of a CLI sample, as the dict an application service returns."""
    model = SAMPLES[sample]
    body = model.body if hasattr(model, "body") else model
    return cast(dict[str, Any], body.model_dump(by_alias=True))  # type: ignore[attr-defined]


_CONNECTION = _wire("get_connection")
_PROJECT = _wire("get_project")
_INGESTION = _wire("get_ingestion")
_ANSWER = cast(Tagged[Any], SAMPLES["create_chat_completion"])
_SCHEDULE = schedule_view(
    Schedule("acme", "engineering-hourly", "engineering", "jira-main", interval_seconds=3600),
    slots=[],
    runs={},
    enabled=True,
)
_SCHEDULE_WRITE = ScheduleWriteRequest.model_validate(
    {"input": {"project_id": "engineering", "connection_id": "jira-main"}, "interval": "PT1H"}
)
_SESSION = {
    "session_id": "ses_1",
    "title": "",
    "project_id": None,
    "profile_id": None,
    "message_count": 0,
    "created_at": _NOW,
    "updated_at": _NOW,
}
_COMPLETION = ChatCompletionResponse.model_validate(
    SAMPLES["create_chat_completion"].body.model_dump()  # type: ignore[attr-defined]
)


@dataclass(frozen=True, slots=True)
class _Case:
    """One operation's call, the application-service call it must make and that call's answer."""

    run: Callable[[LocalOperations], Awaitable[object]]
    service_call: str | None
    answer: object = None


_CASES: dict[str, _Case] = {
    "me": _Case(lambda ops: ops.me(_SCOPE.principal, tenant_id="acme"), None),
    "list_source_types": _Case(lambda ops: ops.list_source_types(_SCOPE, category=None), None),
    "list_connections": _Case(
        lambda ops: ops.list_connections(
            _SCOPE,
            project_id=None,
            source_type=None,
            status=None,
            scheduled=None,
            limit=20,
            cursor=None,
        ),
        "connection_service.list_page",
        ([_CONNECTION], None),
    ),
    "get_connection": _Case(
        lambda ops: ops.get_connection(_SCOPE, _REF),
        "connection_service.get",
        (_CONNECTION, 1),
    ),
    "put_connection": _Case(
        lambda ops: ops.put_connection(
            _SCOPE,
            _REF,
            ConnectionWriteRequest(
                source_type="jira", name="Jira", credentials={}, config={"jql": "project = DEMO"}
            ),
            if_match=None,
            if_none_match=None,
        ),
        "connection_service.put",
        SimpleNamespace(connection=_CONNECTION, version=1, created=True),
    ),
    "delete_connection": _Case(
        lambda ops: ops.delete_connection(_SCOPE, _REF, purge=False),
        "connection_service.delete",
        "job_1",
    ),
    "test_connection": _Case(
        lambda ops: ops.test_connection(_SCOPE, _REF, None),
        "connection_service.test",
        SAMPLES["test_connection"].model_dump(by_alias=True),  # type: ignore[attr-defined]
    ),
    "list_projects": _Case(
        lambda ops: ops.list_projects(_SCOPE, q=None, limit=20, cursor=None),
        "project_service.list_page",
        ([_PROJECT], None),
    ),
    "get_project": _Case(
        lambda ops: ops.get_project(_SCOPE, "engineering"), "project_service.get", (_PROJECT, 1)
    ),
    "put_project": _Case(
        lambda ops: ops.put_project(
            _SCOPE,
            "engineering",
            ProjectWriteRequest(project_name="Engineering", project_description=None),
            if_match=None,
            if_none_match=None,
        ),
        "project_service.put",
        (_PROJECT, True, 1),
    ),
    "delete_project": _Case(
        lambda ops: ops.delete_project(_SCOPE, "engineering", cascade=False),
        "project_service.delete",
        None,
    ),
    "list_searchable_connections": _Case(
        lambda ops: ops.list_searchable_connections(_SCOPE, "engineering"),
        "connection_service.searchable",
        [],
    ),
    "start_ingestion": _Case(
        lambda ops: ops.start_ingestion(
            _SCOPE, IngestionStartRequest(project_id="engineering", connection_id="jira-main")
        ),
        "ingestion_starter.start",
        _INGESTION,
    ),
    "list_ingestions": _Case(
        lambda ops: ops.list_ingestions(_SCOPE, IngestionListQuery()),
        "ingestion_operations.list",
        {"items": [_INGESTION], "next_cursor": None},
    ),
    "get_ingestion": _Case(
        lambda ops: ops.get_ingestion(_SCOPE, "ing_1"), "ingestion_operations.get", _INGESTION
    ),
    "list_ingestion_documents": _Case(
        lambda ops: ops.list_ingestion_documents(_SCOPE, "ing_1", IngestionDocumentQuery()),
        "ingestion_operations.documents",
        {"items": [], "next_cursor": None},
    ),
    "stream_ingestion": _Case(
        lambda ops: ops.stream_ingestion(_SCOPE, "ing_1", after_seq=None),
        "ingestion_follower.follow",
        None,
    ),
    "pause_ingestion": _Case(
        lambda ops: ops.pause_ingestion(_SCOPE, "ing_1"), "ingestion_operations.act", _INGESTION
    ),
    "resume_ingestion": _Case(
        lambda ops: ops.resume_ingestion(_SCOPE, "ing_1"), "ingestion_operations.act", _INGESTION
    ),
    "cancel_ingestion": _Case(
        lambda ops: ops.cancel_ingestion(_SCOPE, "ing_1"), "ingestion_operations.act", _INGESTION
    ),
    "retry_ingestion_failures": _Case(
        lambda ops: ops.retry_ingestion_failures(_SCOPE, "ing_1", None),
        "ingestion_operations.retry_failures",
        _INGESTION,
    ),
    "list_failures": _Case(
        lambda ops: ops.list_failures(_SCOPE, FailureQuery()),
        "failure_inbox.page",
        {"items": [], "next_cursor": None, "counts": {}, "total": 0},
    ),
    "retry_failures": _Case(
        lambda ops: ops.retry_failures(_SCOPE, FailureRetryRequest(ids=["f1"])),
        "failure_inbox.retry",
        {"ingestion_id": "ing_2", "ingestion_ids": ["ing_2"], "queued": 1},
    ),
    "list_schedules": _Case(
        lambda ops: ops.list_schedules(_SCOPE, ScheduleListQuery()),
        "schedule_service.page",
        {"items": [_SCHEDULE], "next_cursor": None},
    ),
    "get_schedule": _Case(
        lambda ops: ops.get_schedule(_SCOPE, "engineering-hourly"),
        "schedule_service.get",
        (_SCHEDULE, 1),
    ),
    "put_schedule": _Case(
        lambda ops: ops.put_schedule(
            _SCOPE, "engineering-hourly", _SCHEDULE_WRITE, preconditions=Preconditions()
        ),
        "schedule_service.put",
        (_SCHEDULE, 1, True),
    ),
    "delete_schedule": _Case(
        lambda ops: ops.delete_schedule(_SCOPE, "engineering-hourly"),
        "schedule_service.delete",
        None,
    ),
    "pause_schedule": _Case(
        lambda ops: ops.pause_schedule(_SCOPE, "engineering-hourly", None),
        "schedule_service.pause",
        (_SCHEDULE, 2),
    ),
    "resume_schedule": _Case(
        lambda ops: ops.resume_schedule(_SCOPE, "engineering-hourly"),
        "schedule_service.resume",
        (_SCHEDULE, 3),
    ),
    "trigger_schedule": _Case(
        lambda ops: ops.trigger_schedule(_SCOPE, "engineering-hourly"),
        "schedule_service.trigger",
        _INGESTION,
    ),
    "read_settings": _Case(
        lambda ops: ops.read_settings(_SCOPE, level=None, effective=False),
        "settings_service.read",
        SettingsRead("tenant", 3, {}),
    ),
    "patch_settings": _Case(
        lambda ops: ops.patch_settings(
            _SCOPE,
            {"chunking.max_tokens": 300},
            level=None,
            dry_run=False,
            reindex=False,
            if_match='W/"rev-3"',
        ),
        "settings_service.patch",
        SettingsPatched(4, False, ()),
    ),
    "settings_schema": _Case(
        lambda ops: ops.settings_schema(_SCOPE), "settings_service.schema", {}
    ),
    "create_chat_completion": _Case(
        lambda ops: ops.create_chat_completion(
            _SCOPE,
            ChatCompletionRequest.model_validate({"message": "Explain the release"}),
            idempotency_key="request-1",
            trace_id="trace-1",
        ),
        "complete_chat_request",
        CompletionOutcome(_COMPLETION.model_dump_json(), replayed=False),
    ),
    "create_chat_session": _Case(
        lambda ops: ops.create_chat_session(_SCOPE, ChatSessionCreateRequest()),
        "start_chat_session",
        AppResponse(True, _SESSION),
    ),
    "list_chat_sessions": _Case(
        lambda ops: ops.list_chat_sessions(_SCOPE, cursor=None, limit=20, q=None),
        "list_conversations",
        AppResponse(True, {"items": [], "next_cursor": None}),
    ),
    "list_conversation_messages": _Case(
        lambda ops: ops.list_conversation_messages(_SCOPE, "ses_1", cursor=None, limit=20),
        "conversation_messages",
        AppResponse(True, {"items": [], "next_cursor": None}),
    ),
}

_ROLE: Role = "owner"


async def test_every_operation_of_the_port_has_a_case() -> None:
    methods = {name for name in dir(Operations) if not name.startswith("_")}

    assert set(_CASES) == methods == set(OPERATIONS)
    assert {name for name, case in _CASES.items() if case.service_call is None} == set(
        NO_SERVICE_CALL
    )


@pytest.mark.parametrize("name", sorted(_CASES))
async def test_an_operation_makes_exactly_its_one_application_service_call(name: str) -> None:
    case = _CASES[name]
    recorder = _Recorder({} if case.service_call is None else {case.service_call: case.answer})

    try:
        await case.run(LocalOperations(cast(Any, recorder)))
    except _Unscripted:
        pass

    assert recorder.calls == ([] if case.service_call is None else [case.service_call])


async def test_the_cli_new_tenant_guard_is_one_application_service_call() -> None:
    recorder = _Recorder({"tenant_has_data": True})

    assert await LocalOperations(cast(Any, recorder)).tenant_has_data("acme") is True
    assert recorder.calls == ["tenant_has_data"]
