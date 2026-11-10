"""LocalOperations runs the settings and chat operations, and every operation checks its role."""

from __future__ import annotations

import inspect
from typing import Any

import pytest
from app_test_fixtures import MockAppService

from harborrag_app.api.auth.principal import ROLE_ORDER, Principal
from harborrag_app.api.v1.chat.dispatch import ModeUnavailableError
from harborrag_app.api.v1.chat.schemas import ChatCompletionRequest, ChatSessionCreateRequest
from harborrag_app.operations import (
    LocalOperations,
    Operations,
    ReferenceNotFoundError,
    Scope,
    Tagged,
)
from harborrag_app.operations.contract import OPERATIONS
from harborrag_app.operations.errors import error_body
from harborrag_app.workflow_control.control_plane.settings_service import (
    SettingsPatched,
    SettingsRead,
)
from harborrag_core.config_store import SettingChange, SettingsLevel
from harborrag_core.contracts.errors import HarborAuthError, HarborValidationError

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

_WORKSPACE = "connection:default/harborrag-workspace"
_QUESTION = ChatCompletionRequest.model_validate({"message": "Explain the release"})
_ROLE_BELOW = {rank + 1: role for role, rank in ROLE_ORDER.items()}


class _Settings:
    """Settings service double that records what the operations asked for."""

    def __init__(self) -> None:
        self.reads: list[SettingsLevel] = []
        self.patches: list[dict[str, Any]] = []

    async def read(self, *, tenant_id: str, level: SettingsLevel, effective: bool) -> SettingsRead:
        self.reads.append(level)
        return SettingsRead(str(level), 3, {"chunking.max_tokens": 384})

    async def patch(self, **request: Any) -> SettingsPatched:
        self.patches.append(request)
        change = SettingChange(
            str(request["level"]), "chunking.max_tokens", 384, 300, "next_ingestion"
        )
        return SettingsPatched(4, request["dry_run"], (change,))

    def schema(self) -> dict[str, Any]:
        return {"groups": []}


class _App(MockAppService):
    """Mock app service with a settings double and one indexed engineering connection."""

    def __init__(self) -> None:
        super().__init__()
        self.settings_service = _Settings()
        self.known_projects = {"engineering"}
        self.catalog.add("engineering", "engineering/jira-main", ("scope-jira",), documents=3)


class _Untouchable:
    """App service stand-in that fails the test when an operation reaches it."""

    def __getattr__(self, name: str) -> object:
        raise AssertionError(f"the operation used the service before its role check: {name}")


def _scope(role: Any = "reader", user_id: str = "alex@example.com") -> Scope:
    return Scope(Principal("u_demo", role, frozenset({"acme"}), user_id=user_id), "acme")


@pytest.fixture
def app() -> _App:
    return _App()


@pytest.fixture
def operations(app: _App) -> LocalOperations:
    return LocalOperations(app)


async def test_an_editor_may_not_read_tenant_settings(
    operations: LocalOperations, app: _App
) -> None:
    with pytest.raises(HarborAuthError) as refused:
        await operations.read_settings(_scope("editor"), level="tenant", effective=False)

    assert error_body(refused.value).status == 403
    assert app.settings_service.reads == []


async def test_an_editor_may_read_a_connections_settings_with_their_etag(
    operations: LocalOperations,
) -> None:
    read = await operations.read_settings(_scope("editor"), level=_WORKSPACE, effective=False)

    assert isinstance(read, Tagged)
    assert read.etag == 'W/"rev-3"'
    assert (read.body.level, read.body.revision) == (_WORKSPACE, "rev_3")


async def test_patch_settings_without_if_match_is_refused_on_that_field(
    operations: LocalOperations, app: _App
) -> None:
    with pytest.raises(HarborValidationError) as refused:
        await operations.patch_settings(
            _scope("admin"),
            {"chunking.max_tokens": 300},
            level="project:engineering",
            dry_run=False,
            reindex=False,
            if_match=None,
        )

    body = error_body(refused.value)
    assert (body.status, body.code) == (422, "validation_error")
    assert body.details == [{"field": "If-Match", "issue": "If-Match is required"}]
    assert app.settings_service.patches == []


async def test_patch_settings_tags_the_new_revision_and_records_the_actor(
    operations: LocalOperations, app: _App
) -> None:
    patched = await operations.patch_settings(
        _scope("admin"),
        {"chunking.max_tokens": 300},
        level="project:engineering",
        dry_run=False,
        reindex=False,
        if_match='W/"rev-3"',
    )

    assert patched.etag == 'W/"rev-4"'
    assert patched.body.revision == "rev_4"
    assert patched.body.changed[0].model_dump(by_alias=True) == {
        "key": "chunking.max_tokens",
        "from": 384,
        "to": 300,
        "applies": "next_ingestion",
    }
    assert app.settings_service.patches[0]["actor"] == "u_demo"
    assert app.settings_service.patches[0]["expected_revision"] == 3


async def test_advanced_mode_is_unavailable_before_any_model_call(
    operations: LocalOperations, app: _App
) -> None:
    advanced = ChatCompletionRequest.model_validate(
        {"message": "Explain the release", "mode": "advanced"}
    )

    with pytest.raises(ModeUnavailableError):
        await operations.create_chat_completion(
            _scope(), advanced, idempotency_key=None, trace_id="trace-demo"
        )

    assert app.chat.requests == []


async def test_a_repeated_idempotency_key_replays_the_same_body(
    operations: LocalOperations, app: _App
) -> None:
    first = await operations.create_chat_completion(
        _scope(), _QUESTION, idempotency_key="request-1", trace_id="trace-1"
    )
    again = await operations.create_chat_completion(
        _scope(), _QUESTION, idempotency_key="request-1", trace_id="trace-2"
    )

    assert (first.replayed, again.replayed) == (False, True)
    assert first.body is not None
    assert again.body is not None
    assert again.body.model_dump_json() == first.body.model_dump_json()
    assert len(app.chat.requests) == 1


async def test_a_streamed_completion_is_framed_as_events(operations: LocalOperations) -> None:
    streamed = ChatCompletionRequest.model_validate(
        {"message": "Explain the release", "stream": True}
    )

    completion = await operations.create_chat_completion(
        _scope(), streamed, idempotency_key=None, trace_id="trace-1"
    )

    assert completion.body is None
    assert completion.events is not None
    frames = [frame async for frame in completion.events]
    assert [frame.split(b"\n", 1)[0] for frame in frames] == [
        b"event: response.started",
        b"event: response.output_text.delta",
        b"event: response.completed",
    ]


async def test_create_chat_session_in_an_unknown_project_names_the_field(
    operations: LocalOperations,
) -> None:
    with pytest.raises(ReferenceNotFoundError) as refused:
        await operations.create_chat_session(
            _scope(), ChatSessionCreateRequest(project_id="nowhere")
        )

    assert refused.value.details["field"] == "project_id"
    assert error_body(refused.value).status == 422


async def test_chat_sessions_are_listed_for_their_own_user_only(
    operations: LocalOperations,
) -> None:
    created = await operations.create_chat_session(
        _scope(), ChatSessionCreateRequest(project_id="engineering")
    )

    own = await operations.list_chat_sessions(_scope(), cursor=None, limit=20, q=None)
    other = await operations.list_chat_sessions(
        _scope(user_id="sam@example.com"), cursor=None, limit=20, q=None
    )
    messages = await operations.list_conversation_messages(
        _scope(), created.session_id, cursor=None, limit=20
    )

    assert [session.session_id for session in own.items] == [created.session_id]
    assert other.items == []
    assert messages.items == []


async def test_the_operations_table_names_every_port_method() -> None:
    methods = {name for name in dir(Operations) if not name.startswith("_")}

    assert methods == set(OPERATIONS)


@pytest.mark.parametrize(
    "name", [name for name, spec in OPERATIONS.items() if spec.role != "reader"]
)
async def test_each_operation_refuses_the_role_below_its_own_before_any_service_call(
    name: str,
) -> None:
    below = _ROLE_BELOW[ROLE_ORDER[OPERATIONS[name].role]]
    method = getattr(LocalOperations(_Untouchable()), name)
    required = [
        parameter.name
        for parameter in list(inspect.signature(method).parameters.values())[1:]
        if parameter.default is inspect.Parameter.empty
    ]

    with pytest.raises(HarborAuthError) as refused:
        await method(_scope(below), **dict.fromkeys(required))

    assert type(refused.value) is HarborAuthError
