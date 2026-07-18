"""Call the real locally hosted chat and embedding deployments and print results.

Usage: uv run python scripts/models/smoke_test_local_models.py
"""

from __future__ import annotations

import asyncio

from harborrag_adapters.models.chat import HarborChatClient, HarborChatClientConfig
from harborrag_adapters.models.embed import HarborEmbedClient, HarborEmbedClientConfig
from harborrag_core.models.chat import HarborChatMessage, StreamEventType

CHAT_CONFIG = {
    "chat": {
        "default_model": "local-chat",
        "models": {
            "local-chat": {
                "deployments": [
                    {
                        "name": "local-chat",
                        "provider": "vllm",
                        "model": "hosted_vllm/Qwen3.5-0.8B-int8-ov",
                        "api_base": "http://localhost:8000/v3",
                    }
                ]
            }
        },
    }
}

EMBED_CONFIG = {
    "embed": {
        "default_model": "local-embed",
        "models": {
            "local-embed": {
                "embedding_space": "local-vllm-smoke",
                "deployments": [
                    {
                        "name": "local-embed",
                        "provider": "vllm",
                        "model": "hosted_vllm/Qwen3-Embedding-0.6B-int8-ov",
                        "api_base": "http://localhost:8001/v3",
                        "expected_dimensions": 1024,
                    }
                ],
            }
        },
    }
}


async def check_chat() -> None:
    config = HarborChatClientConfig.from_dict(CHAT_CONFIG)
    async with HarborChatClient.from_config(config) as client:
        response = await client.achat(
            [
                HarborChatMessage.system("Return a brief plain-text answer."),
                HarborChatMessage.user("Reply with: harbor-chat-smoke-ok"),
            ]
        )
    print("[chat] provider_model:", response.provider_model)
    print("[chat] request_id:", response.request_id)
    print("[chat] latency_ms:", response.latency_ms)
    print("[chat] text:", response.text)


async def check_chat_stream() -> None:
    config = HarborChatClientConfig.from_dict(CHAT_CONFIG)
    text_chunks: list[str] = []
    async with HarborChatClient.from_config(config) as client:
        async for chunk in client.astream(
            [
                HarborChatMessage.system("Return a brief plain-text answer."),
                HarborChatMessage.user("Count from 1 to 5, one number per word."),
            ]
        ):
            if chunk.event is StreamEventType.TEXT_DELTA and chunk.text_delta:
                text_chunks.append(chunk.text_delta)
                print("[stream] delta:", repr(chunk.text_delta))
            elif chunk.event is StreamEventType.COMPLETED:
                print("[stream] completed, finish_reason:", chunk.finish_reason)
            elif chunk.event is StreamEventType.ERROR:
                print("[stream] error:", chunk.error)
    print("[stream] full text:", "".join(text_chunks))


async def check_embed() -> None:
    config = HarborEmbedClientConfig.from_dict(EMBED_CONFIG)
    async with HarborEmbedClient(config) as client:
        response = await client.aembed(
            [
                "HarborRAG embedding smoke test.",
                "Neural operators learn mappings between function spaces.",
            ]
        )
    print("[embed] provider_model:", response.provider_model)
    print("[embed] request_id:", response.request_id)
    print("[embed] dimensions:", response.dimensions)
    print("[embed] vector count:", len(response.embeddings))
    print("[embed] first vector head:", response.vectors[0][:5])


async def main() -> None:
    await check_chat()
    print()
    await check_chat_stream()
    print()
    await check_embed()


if __name__ == "__main__":
    asyncio.run(main())
