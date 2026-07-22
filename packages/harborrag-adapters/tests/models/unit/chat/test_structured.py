from __future__ import annotations

from collections.abc import Callable

import pytest
from pydantic import BaseModel, Field

from harborrag_adapters.models.chat import HarborChatClient
from harborrag_core.models.capabilities import HarborChatCapabilities
from harborrag_core.models.chat import HarborChatMessage, StructuredOutputDegradation
from harborrag_core.models.errors import (
    HarborChatCapabilityError,
    HarborChatInvalidRequestError,
    HarborChatStructuredOutputError,
)

from .chat_client_support import FakeInvocation, response_dict

pytestmark = [pytest.mark.unit, pytest.mark.graybox]


class TypedAnswer(BaseModel):
    answer: str
    confidence: float = Field(ge=0, le=1)


class InvalidSchemaModel(BaseModel):
    callback: Callable[[int], int]


def _configured(
    base_config,
    capabilities: HarborChatCapabilities,
    *,
    degradation: StructuredOutputDegradation = StructuredOutputDegradation.JSON_MODE,
    repairs: int = 1,
):
    logical = base_config.models["primary"]
    deployment = logical.deployments[0].model_copy(update={"capabilities": capabilities})
    structured_output = base_config.structured_output.model_copy(
        update={"degradation": degradation, "max_repair_attempts": repairs}
    )
    return base_config.model_copy(
        update={
            "models": {
                **base_config.models,
                "primary": logical.model_copy(update={"deployments": (deployment,)}),
            },
            "structured_output": structured_output,
        }
    )


def test_native_structured_output_returns_validated_pydantic_model(base_config) -> None:
    config = _configured(
        base_config,
        HarborChatCapabilities(structured_output=True, json_mode=True),
    )
    invocation = FakeInvocation([response_dict('{"answer":"yes","confidence":0.9}')])

    result = HarborChatClient(config, invocation=invocation).chat_structured(
        [HarborChatMessage.user("question")],
        response_model=TypedAnswer,
    )

    assert result == TypedAnswer(answer="yes", confidence=0.9)
    response_format = invocation.calls[0]["response_format"]
    assert response_format["type"] == "json_schema"
    assert response_format["json_schema"]["name"] == "TypedAnswer"
    assert response_format["json_schema"]["strict"] is True
    assert "confidence" in response_format["json_schema"]["schema"]["properties"]


@pytest.mark.asyncio
async def test_async_structured_output_uses_the_same_validation_path(
    base_config,
) -> None:
    config = _configured(base_config, HarborChatCapabilities(structured_output=True))
    invocation = FakeInvocation([response_dict('{"answer":"async","confidence":0.7}')])

    result = await HarborChatClient(config, invocation=invocation).achat_structured(
        [HarborChatMessage.user("question")],
        response_model=TypedAnswer,
    )

    assert result.answer == "async"
    assert invocation.async_calls[0]["response_format"]["type"] == "json_schema"


def test_invalid_json_raises_explicit_structured_output_error(base_config) -> None:
    config = _configured(base_config, HarborChatCapabilities(structured_output=True), repairs=0)
    client = HarborChatClient(config, invocation=FakeInvocation([response_dict("not-json")]))

    with pytest.raises(HarborChatStructuredOutputError) as captured:
        client.chat_structured(
            [HarborChatMessage.user("question")],
            response_model=TypedAnswer,
        )

    assert captured.value.metadata == {
        "response_model": "TypedAnswer",
        "completion_attempts": 1,
    }


def test_schema_mismatch_is_never_returned_as_valid_data(base_config) -> None:
    config = _configured(base_config, HarborChatCapabilities(structured_output=True), repairs=0)
    client = HarborChatClient(
        config,
        invocation=FakeInvocation([response_dict('{"answer":"missing confidence"}')]),
    )

    with pytest.raises(HarborChatStructuredOutputError, match="validation failed"):
        client.chat_structured(
            [HarborChatMessage.user("question")],
            response_model=TypedAnswer,
        )


def test_one_bounded_repair_can_recover_invalid_output(base_config) -> None:
    config = _configured(base_config, HarborChatCapabilities(structured_output=True), repairs=1)
    invocation = FakeInvocation(
        [
            response_dict('{"answer":"missing confidence"}'),
            response_dict('{"answer":"repaired","confidence":1.0}'),
        ]
    )

    result = HarborChatClient(config, invocation=invocation).chat_structured(
        [HarborChatMessage.user("question")],
        response_model=TypedAnswer,
    )

    assert result.answer == "repaired"
    assert len(invocation.calls) == 2
    repair_messages = invocation.calls[1]["messages"]
    assert repair_messages[-2]["role"] == "assistant"
    assert "failed JSON schema validation" in repair_messages[-1]["content"]
    assert invocation.calls[1]["response_format"]["type"] == "json_schema"


def test_repair_exhaustion_raises_after_the_configured_bound(base_config) -> None:
    config = _configured(base_config, HarborChatCapabilities(structured_output=True), repairs=1)
    invocation = FakeInvocation([response_dict("invalid-1"), response_dict("invalid-2")])

    with pytest.raises(HarborChatStructuredOutputError) as captured:
        HarborChatClient(config, invocation=invocation).chat_structured(
            [HarborChatMessage.user("question")],
            response_model=TypedAnswer,
        )

    assert captured.value.metadata["completion_attempts"] == 2
    assert len(invocation.calls) == 2


def test_json_mode_is_used_when_native_schema_is_not_declared(base_config) -> None:
    config = _configured(base_config, HarborChatCapabilities(json_mode=True))
    invocation = FakeInvocation([response_dict('{"answer":"json","confidence":0.6}')])

    HarborChatClient(config, invocation=invocation).chat_structured(
        [HarborChatMessage.user("question")],
        response_model=TypedAnswer,
    )

    assert invocation.calls[0]["response_format"] == {"type": "json_object"}


def test_prompt_fallback_requires_explicit_policy(base_config) -> None:
    config = _configured(
        base_config,
        HarborChatCapabilities(),
        degradation=StructuredOutputDegradation.PROMPT,
    )
    invocation = FakeInvocation([response_dict('{"answer":"prompt","confidence":0.5}')])

    HarborChatClient(config, invocation=invocation).chat_structured(
        [HarborChatMessage.user("question")],
        response_model=TypedAnswer,
    )

    parameters = invocation.calls[0]
    assert "response_format" not in parameters
    assert parameters["messages"][0]["role"] == "system"
    assert "JSON Schema" in parameters["messages"][0]["content"]


def test_unsupported_structured_output_stops_before_invocation(base_config) -> None:
    config = _configured(base_config, HarborChatCapabilities())
    invocation = FakeInvocation([response_dict()])

    with pytest.raises(HarborChatCapabilityError, match="structured-output strategy"):
        HarborChatClient(config, invocation=invocation).chat_structured(
            [HarborChatMessage.user("question")],
            response_model=TypedAnswer,
        )

    assert invocation.calls == []


def test_unrepresentable_response_schema_raises_structured_error(base_config) -> None:
    config = _configured(base_config, HarborChatCapabilities(structured_output=True))
    invocation = FakeInvocation([response_dict()])

    with pytest.raises(HarborChatStructuredOutputError, match="JSON Schema"):
        HarborChatClient(config, invocation=invocation).chat_structured(
            [HarborChatMessage.user("question")],
            response_model=InvalidSchemaModel,
        )

    assert invocation.calls == []


def test_repair_override_is_strictly_bounded(base_config) -> None:
    config = _configured(base_config, HarborChatCapabilities(structured_output=True))
    client = HarborChatClient(config, invocation=FakeInvocation())

    with pytest.raises(HarborChatInvalidRequestError, match="max_repair_attempts"):
        client.chat_structured(
            [HarborChatMessage.user("question")],
            response_model=TypedAnswer,
            max_repair_attempts=4,
        )
