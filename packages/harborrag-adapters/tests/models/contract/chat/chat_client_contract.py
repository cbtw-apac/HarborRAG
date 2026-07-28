from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from typing import Any, Protocol

from pydantic import BaseModel

from harborrag_core.models.chat import HarborChatMessage, StreamEventType


class ContractAnswer(BaseModel):
    answer: str


class ChatClientContractHarness(Protocol):
    client: object

    async def complete(self) -> object: ...

    async def stream(self) -> tuple[object, ...]: ...

    async def structured(self) -> ContractAnswer: ...

    async def close(self) -> None: ...


class ChatClientContractSuite:
    """Verify behavior shared by every synchronous and asynchronous chat client."""

    async def verify_completion(self, harness: ChatClientContractHarness) -> None:
        response = await harness.complete()

        assert response.text == "contract"
        assert response.logical_model == "primary"
        assert response.usage.total_tokens == 2

    async def verify_streaming(self, harness: ChatClientContractHarness) -> None:
        events = await harness.stream()

        assert [event.event for event in events] == [
            StreamEventType.METADATA,
            StreamEventType.TEXT_DELTA,
            StreamEventType.METADATA,
            StreamEventType.COMPLETED,
        ]
        assert events[1].text_delta == "stream"
        assert events[-1].finish_reason == "stop"

    async def verify_structured_output(
        self,
        harness: ChatClientContractHarness,
    ) -> None:
        assert await harness.structured() == ContractAnswer(answer="typed")

    async def verify_lifecycle(self, harness: ChatClientContractHarness) -> None:
        await harness.close()
        await harness.close()
        try:
            await harness.complete()
        except RuntimeError as exc:
            assert "closed" in str(exc)
        else:
            raise AssertionError("closed chat client accepted a completion")


@dataclass
class ContractInvocation:
    responses: list[dict[str, Any]]
    streams: list[list[dict[str, Any]]]
    close_count: int = 0

    def complete(self, **kwargs: Any) -> dict[str, Any]:
        del kwargs
        return self.responses.pop(0)

    async def acomplete(self, **kwargs: Any) -> dict[str, Any]:
        return self.complete(**kwargs)

    def stream(self, **kwargs: Any) -> Iterator[dict[str, Any]]:
        del kwargs
        return iter(self.streams.pop(0))

    async def astream(self, **kwargs: Any) -> AsyncIterator[dict[str, Any]]:
        del kwargs
        items = self.streams.pop(0)

        async def generate() -> AsyncIterator[dict[str, Any]]:
            for item in items:
                yield item

        return generate()

    def close_stream(self, stream: object) -> None:
        close = getattr(stream, "close", None)
        if callable(close):
            close()

    async def aclose_stream(self, stream: object) -> None:
        close = getattr(stream, "aclose", None)
        if callable(close):
            await close()

    def close(self) -> None:
        self.close_count += 1

    async def aclose(self) -> None:
        self.close_count += 1


def response(text: str) -> dict[str, Any]:
    return {
        "id": f"response-{text}",
        "model": "provider-model",
        "choices": [
            {
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": text},
            }
        ],
        "usage": {
            "prompt_tokens": 1,
            "completion_tokens": 1,
            "total_tokens": 2,
        },
    }


def stream_chunks() -> list[dict[str, Any]]:
    return [
        {
            "id": "stream",
            "model": "provider-model",
            "choices": [
                {
                    "delta": {"content": "stream"},
                    "finish_reason": None,
                }
            ],
        },
        {
            "id": "stream",
            "model": "provider-model",
            "choices": [
                {
                    "delta": {},
                    "finish_reason": "stop",
                }
            ],
        },
    ]


CONTRACT_MESSAGES = (HarborChatMessage.user("contract"),)
