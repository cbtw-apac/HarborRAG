from __future__ import annotations

import pytest
from model_runtime_support import chat_config

from harborrag_adapters.models.chat.streaming import ChatStreamNormalizer
from harborrag_core.models.chat import StreamEventType

pytestmark = [pytest.mark.unit, pytest.mark.whitebox]


def test_stream_normalizes_reasoning_tools_usage_and_finish_metadata() -> None:
    deployment = chat_config().models["primary"].deployments[0]
    normalizer = ChatStreamNormalizer(
        logical_model="primary",
        deployment=deployment,
        request_id="request-1",
    )
    chunks = [
        {
            "id": "response-1",
            "model": "provider-model",
            "choices": [
                {
                    "delta": {
                        "reasoning_content": "Think ",
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call-1",
                                "function": {"name": "lookup", "arguments": '{"q":'},
                            }
                        ],
                    }
                }
            ],
        },
        {
            "id": "response-1",
            "model": "provider-model",
            "choices": [
                {
                    "delta": {
                        "reasoning_content": "carefully.",
                        "tool_calls": [{"index": 0, "function": {"arguments": '"rag"}'}}],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
        },
        {
            "id": "response-1",
            "model": "provider-model",
            "choices": [],
            "usage": {"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7},
        },
    ]

    events = [event for chunk in chunks for event in normalizer.consume(chunk)]
    completed = normalizer.complete()

    assert [event.reasoning_delta for event in events if event.reasoning_delta] == [
        "Think ",
        "carefully.",
    ]
    assert completed.tool_calls[0].function.name == "lookup"
    assert completed.tool_calls[0].function.parsed_arguments == {"q": "rag"}
    assert completed.usage is not None and completed.usage.total_tokens == 7
    assert completed.finish_reason == "tool_calls"
    assert completed.metadata["finish_reason"] == "tool_calls"
    assert completed.metadata["first_token_latency_ms"] >= 0
    assert completed.metadata["stream_duration_ms"] >= 0
    assert StreamEventType.METADATA in {event.event for event in events}
