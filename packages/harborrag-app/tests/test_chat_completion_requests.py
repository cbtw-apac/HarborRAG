"""complete_chat_request refuses before any model call and replays a repeated idempotency key."""

from __future__ import annotations

import json

import pytest
from app_test_fixtures import MockAppService

from harborrag_core.contracts.errors import (
    CompletionInProgressError,
    HarborConflictError,
    HarborValidationError,
    ModeUnavailableError,
    UnknownReferenceError,
)
from harborrag_runtime.application.chat.completion_request import (
    CompletionCaller,
    CompletionRequest,
    ReplayKey,
    RequestedMode,
)
from harborrag_runtime.application.chat.replay import CompletionAttempt
from harborrag_runtime.application.chat.request_filters import FilterCondition, FilterGroup

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

_CALLER = CompletionCaller("DEFAULT", "u_demo", "alex@example.com", "u_demo")
_HASH = "hash-of-the-body"


def _accepted(body: str) -> None:
    del body


def _rejected(body: str) -> None:
    raise ValueError("the wire model does not accept this body")


@pytest.fixture
def service() -> MockAppService:
    double = MockAppService()
    double.known_projects = {"engineering"}
    double.catalog.add("engineering", "engineering/jira-main", ("scope-jira",), documents=3)
    return double


def _request(
    *,
    mode: RequestedMode = "classic",
    filters: FilterGroup | None = None,
    connections: tuple[str, ...] = (),
) -> CompletionRequest:
    return CompletionRequest(
        message="Explain the release",
        session_id=None,
        mode=mode,
        filters=filters,
        project_ids=("engineering",),
        connections=connections,
        trace_id="trace-demo",
    )


def _connection_filter(op: str, value: object) -> FilterGroup:
    return FilterGroup("and", (FilterCondition("connection", op, value),))


@pytest.mark.parametrize("mode", ["advanced", "auto"])
async def test_an_unavailable_mode_is_refused_before_any_model_call(
    service: MockAppService, mode: RequestedMode
) -> None:
    with pytest.raises(ModeUnavailableError) as refused:
        await service.complete_chat_request(
            _request(mode=mode),
            caller=_CALLER,
            replay=ReplayKey(None, _HASH),
            validate_body=_accepted,
        )

    assert refused.value.details == {"field": "mode"}
    assert service.chat.requests == []


async def test_the_mode_is_refused_before_the_filters_are_read(service: MockAppService) -> None:
    either = FilterGroup("or", ())

    with pytest.raises(ModeUnavailableError):
        await service.complete_chat_request(
            _request(mode="advanced", filters=either),
            caller=_CALLER,
            replay=ReplayKey(None, _HASH),
            validate_body=_accepted,
        )


async def test_an_or_filter_is_refused_on_the_filters_field(service: MockAppService) -> None:
    with pytest.raises(HarborValidationError) as refused:
        await service.complete_chat_request(
            _request(filters=FilterGroup("or", ())),
            caller=_CALLER,
            replay=ReplayKey(None, _HASH),
            validate_body=_accepted,
        )

    assert refused.value.details == {"field": "filters"}
    assert service.chat.requests == []


async def test_a_connection_filter_that_excludes_every_requested_connection_is_refused(
    service: MockAppService,
) -> None:
    with pytest.raises(HarborValidationError) as refused:
        await service.complete_chat_request(
            _request(
                filters=_connection_filter("eq", "engineering/jira-main"),
                connections=("engineering/wiki",),
            ),
            caller=_CALLER,
            replay=ReplayKey(None, _HASH),
            validate_body=_accepted,
        )

    assert refused.value.details == {"field": "filters"}


async def test_a_repeated_key_replays_the_stored_completion_without_a_model_call(
    service: MockAppService,
) -> None:
    replay = ReplayKey("request-1", _HASH)

    first = await service.complete_chat_request(
        _request(), caller=_CALLER, replay=replay, validate_body=_accepted
    )
    again = await service.complete_chat_request(
        _request(), caller=_CALLER, replay=replay, validate_body=_accepted
    )

    assert (first.replayed, again.replayed) == (False, True)
    assert again.body == first.body
    assert json.loads(first.body)["answer"]
    assert len(service.chat.requests) == 1


async def test_a_body_the_wire_model_rejects_is_never_stored_as_a_completed_answer(
    service: MockAppService,
) -> None:
    replay = ReplayKey("request-1", _HASH)

    with pytest.raises(ValueError, match="wire model"):
        await service.complete_chat_request(
            _request(), caller=_CALLER, replay=replay, validate_body=_rejected
        )

    with pytest.raises(HarborConflictError, match="failed"):
        await service.complete_chat_request(
            _request(), caller=_CALLER, replay=replay, validate_body=_accepted
        )


async def test_a_key_reused_with_another_body_is_refused(service: MockAppService) -> None:
    await service.complete_chat_request(
        _request(), caller=_CALLER, replay=ReplayKey("request-1", _HASH), validate_body=_accepted
    )

    with pytest.raises(HarborValidationError) as refused:
        await service.complete_chat_request(
            _request(),
            caller=_CALLER,
            replay=ReplayKey("request-1", "another-body"),
            validate_body=_accepted,
        )

    assert refused.value.details == {"field": "Idempotency-Key"}


async def test_a_duplicate_while_the_first_runs_is_session_busy(service: MockAppService) -> None:
    first = CompletionAttempt(
        service.conversations, "DEFAULT", "alex@example.com", "concurrent-1", _HASH
    )
    assert await first.claim() is None

    with pytest.raises(CompletionInProgressError) as busy:
        await service.complete_chat_request(
            _request(),
            caller=_CALLER,
            replay=ReplayKey("concurrent-1", _HASH),
            validate_body=_accepted,
        )

    assert busy.value.error_code == "session_busy"


async def test_a_session_of_an_unknown_project_is_a_reference_error_naming_the_field(
    service: MockAppService,
) -> None:
    with pytest.raises(UnknownReferenceError) as refused:
        await service.start_chat_session(
            tenant_id="DEFAULT",
            principal_id="u_demo",
            user_id="alex@example.com",
            title=None,
            project_id="nowhere",
        )

    assert refused.value.details == {"field": "project_id"}
