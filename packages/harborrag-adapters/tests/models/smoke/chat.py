from __future__ import annotations

import asyncio

from bootstrap import load_env, safe_error
from config import SmokeNotConfigured, chat_config

from harborrag_adapters.models.chat import ChatClientFactory
from harborrag_core.models.chat import HarborChatMessage


async def _run() -> None:
    async with ChatClientFactory.create_async(chat_config()) as client:
        response = await client.achat(
            [
                HarborChatMessage.system("Return a brief plain-text answer."),
                HarborChatMessage.user("Reply with: harbor-chat-smoke-ok"),
            ]
        )
    if not response.text or not response.text.strip():
        raise AssertionError("provider returned an empty chat response")
    if not response.provider_model or not response.request_id:
        raise AssertionError("chat response is missing provider metadata")
    print(
        "[models/chat] passed "
        f"provider_model={response.provider_model!r} latency_ms={response.latency_ms}"
    )
    print(f"[models/chat] response: {response.text.strip()!r}")


def main() -> int:
    load_env()
    try:
        asyncio.run(_run())
    except SmokeNotConfigured as exc:
        print(f"[models/chat] not configured: {safe_error(exc)}")
        return 2
    except Exception as exc:  # noqa: BLE001
        print(f"[models/chat] failed: {safe_error(exc)}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
