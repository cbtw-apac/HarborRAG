"""Provider controls and frozen-extraction cache boundaries."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from harborrag_adapters.models.chat.configs import ChatReasoningConfig, HarborChatClientConfig
from harborrag_adapters.models.runtime.cache import InMemoryModelCache
from harborrag_adapters.models.runtime.config import CacheConfig
from harborrag_core.models.capabilities import HarborChatCapabilities
from harborrag_core.models.chat import HarborChatMessage
from harborrag_core.models.errors import (
    HarborChatCapabilityError,
    HarborChatInvalidRequestError,
    HarborChatProviderError,
)

from .chat_client_support import FakeInvocation, response_dict, sync_client

pytestmark = [pytest.mark.unit, pytest.mark.graybox]


def configured(
    base: HarborChatClientConfig, **deployment_changes: object
) -> HarborChatClientConfig:
    logical = base.models["primary"]
    deployment = logical.deployments[0].model_copy(update=deployment_changes)
    return base.model_copy(
        update={"models": {"primary": logical.model_copy(update={"deployments": (deployment,)})}}
    )


def test_thinking_controls_are_narrow_and_disabled_by_default(base_config) -> None:
    messages = [HarborChatMessage.user("extract")]
    default_backend = FakeInvocation([response_dict("ok")])
    sync_client(base_config, backend=default_backend).chat(messages)
    assert "extra_body" not in default_backend.calls[0]

    backend = FakeInvocation([response_dict("ok")])
    config = configured(base_config, reasoning=ChatReasoningConfig(enable_thinking=False))
    sync_client(config, backend=backend).chat(messages)
    assert backend.calls[0]["extra_body"] == {"chat_template_kwargs": {"enable_thinking": False}}
    assert "extra_body" not in config.security.allowed_extra_litellm_params


@pytest.mark.parametrize("value", ["false", 0, {}, []])
def test_thinking_controls_reject_coercion(value) -> None:
    with pytest.raises(ValidationError):
        ChatReasoningConfig(enable_thinking=value)


def test_body_extensions_cannot_override_typed_thinking_config(base_config) -> None:
    data = base_config.model_dump(mode="python")
    deployment = data["models"]["primary"]["deployments"][0]
    deployment["reasoning"] = {"enable_thinking": False}
    deployment["extra_litellm_params"] = {
        "extra_body": {"chat_template_kwargs": {"enable_thinking": True}}
    }
    with pytest.raises(ValidationError, match="cannot be combined"):
        HarborChatClientConfig.model_validate(data)


def test_reasoning_effort_is_independent_from_reasoning_content(base_config) -> None:
    request = [HarborChatMessage.user("extract")]
    content_only = configured(
        base_config, capabilities=HarborChatCapabilities(reasoning_content=True)
    )
    with pytest.raises(HarborChatCapabilityError, match="reasoning effort"):
        sync_client(content_only, backend=FakeInvocation()).chat(request, reasoning_effort="low")

    effort = configured(base_config, capabilities=HarborChatCapabilities(reasoning_effort=True))
    backend = FakeInvocation([response_dict("ok")])
    sync_client(effort, backend=backend).chat(request, reasoning_effort="low")
    assert backend.calls[0]["reasoning_effort"] == "low"


def test_approved_request_extensions_still_cannot_override_thinking(base_config) -> None:
    config = configured(base_config, reasoning=ChatReasoningConfig(enable_thinking=False))
    config = config.model_copy(
        update={
            "security": config.security.model_copy(
                update={"allowed_extra_litellm_params": frozenset({"extra_body"})}
            )
        }
    )
    with pytest.raises(HarborChatInvalidRequestError, match="cannot override"):
        sync_client(config, backend=FakeInvocation()).chat(
            [HarborChatMessage.user("extract")], extra_params={"extra_body": {}}
        )


@pytest.mark.parametrize(
    ("content", "expected", "reasoning"),
    [
        (' \n<think>analysis</think>\n{"entity":"A"}', '{"entity":"A"}', "analysis"),
        ('<think></think>{"entity":"A"}', '{"entity":"A"}', None),
        ('{"example":"<think>literal</think>"}', '{"example":"<think>literal</think>"}', None),
    ],
)
def test_only_leading_think_blocks_are_parsed(base_config, content, expected, reasoning) -> None:
    config = configured(base_config, reasoning=ChatReasoningConfig(parse_think_tags=True))
    backend = FakeInvocation([response_dict(content)])
    result = sync_client(config, backend=backend).chat([HarborChatMessage.user("extract")])
    assert result.text == expected
    assert result.reasoning_content == reasoning


def test_inline_thinking_is_unchanged_without_opt_in(base_config) -> None:
    content = "<think>example</think>answer"
    backend = FakeInvocation([response_dict(content)])
    result = sync_client(base_config, backend=backend).chat([HarborChatMessage.user("extract")])
    assert result.text == content


def test_truncated_thinking_is_rejected_without_retry(base_config) -> None:
    config = configured(base_config, reasoning=ChatReasoningConfig(parse_think_tags=True))
    backend = FakeInvocation([response_dict("<think>unfinished")])
    with pytest.raises(HarborChatProviderError, match="unterminated"):
        sync_client(config, backend=backend).chat([HarborChatMessage.user("extract")])
    assert len(backend.calls) == 1


def test_chat_cache_partitions_deployment_changes_but_reuses_identical_configs(base_config) -> None:
    shared_cache = InMemoryModelCache(max_entries=8)
    base = base_config.model_copy(update={"cache": CacheConfig(enabled=True)})
    other = configured(base, model="openai/other-extractor")
    first_backend = FakeInvocation([response_dict("old")])
    other_backend = FakeInvocation([response_dict("new")])
    first = sync_client(base, cache=shared_cache, backend=first_backend)
    same = sync_client(base, cache=shared_cache, backend=FakeInvocation())
    changed = sync_client(other, cache=shared_cache, backend=other_backend)
    messages = [HarborChatMessage.user("same extraction prompt")]
    kwargs = {"cacheable": True, "metadata": {"tenant_id": "tenant-a"}}

    assert first.chat(messages, **kwargs).text == "old"
    assert same.chat(messages, **kwargs).cache_hit
    assert changed.chat(messages, **kwargs).text == "new"
    assert len(other_backend.calls) == 1
