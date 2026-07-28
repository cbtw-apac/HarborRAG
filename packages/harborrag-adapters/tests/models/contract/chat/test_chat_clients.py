from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import pytest
from chat_client_contract import (
    CONTRACT_MESSAGES,
    ChatClientContractSuite,
    ContractAnswer,
    ContractInvocation,
    response,
    stream_chunks,
)

from harborrag_adapters.models.chat import (
    AsyncHarborChatClient,
    ChatClientDependencies,
    ChatClientFactory,
    HarborChatClient,
    HarborChatClientConfig,
)
from harborrag_core.ports.model_clients import (
    AsyncHarborChatClientProtocol,
    HarborChatClientProtocol,
)

pytestmark = pytest.mark.contract


@dataclass
class SyncClientHarness:
    client: HarborChatClient

    async def complete(self) -> object:
        return self.client.chat(CONTRACT_MESSAGES)

    async def stream(self) -> tuple[object, ...]:
        return tuple(self.client.stream(CONTRACT_MESSAGES))

    async def structured(self) -> ContractAnswer:
        return self.client.chat_structured(
            CONTRACT_MESSAGES,
            response_model=ContractAnswer,
        )

    async def close(self) -> None:
        self.client.close()


@dataclass
class AsyncClientHarness:
    client: AsyncHarborChatClient

    async def complete(self) -> object:
        return await self.client.achat(CONTRACT_MESSAGES)

    async def stream(self) -> tuple[object, ...]:
        events = [event async for event in self.client.astream(CONTRACT_MESSAGES)]
        return tuple(events)

    async def structured(self) -> ContractAnswer:
        return await self.client.achat_structured(
            CONTRACT_MESSAGES,
            response_model=ContractAnswer,
        )

    async def close(self) -> None:
        await self.client.aclose()


def contract_config() -> HarborChatClientConfig:
    return HarborChatClientConfig.from_dict(
        {
            "default_model": "primary",
            "models": {
                "primary": {
                    "provider": "openai",
                    "model": "openai/gpt-contract",
                    "api_key": "contract-key",
                    "capabilities": {
                        "streaming": True,
                        "structured_output": True,
                        "json_mode": True,
                    },
                }
            },
        }
    )


def build_harness(
    mode: Literal["sync", "async"],
) -> tuple[SyncClientHarness | AsyncClientHarness, ContractInvocation]:
    invocation = ContractInvocation(
        responses=[response("contract"), response('{"answer":"typed"}')],
        streams=[stream_chunks()],
    )
    dependencies = ChatClientDependencies(backend=invocation)
    if mode == "sync":
        client = ChatClientFactory.create(contract_config(), dependencies)
        assert isinstance(client, HarborChatClientProtocol)
        assert not isinstance(client, AsyncHarborChatClientProtocol)
        return SyncClientHarness(client), invocation

    client = ChatClientFactory.create_async(contract_config(), dependencies)
    assert isinstance(client, AsyncHarborChatClientProtocol)
    assert not isinstance(client, HarborChatClientProtocol)
    return AsyncClientHarness(client), invocation


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["sync", "async"])
async def test_chat_client_contract(mode: Literal["sync", "async"]) -> None:
    harness, invocation = build_harness(mode)
    suite = ChatClientContractSuite()

    await suite.verify_completion(harness)
    await suite.verify_streaming(harness)
    await suite.verify_structured_output(harness)
    await suite.verify_lifecycle(harness)

    assert invocation.close_count == 1
