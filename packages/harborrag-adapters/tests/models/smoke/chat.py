from __future__ import annotations

import asyncio

from bootstrap import load_env, safe_error, set_env_overrides
from config import SmokeNotConfigured, chat_config

from harborrag_adapters.models.chat import ChatClientFactory
from harborrag_core.models.chat import HarborChatMessage

CHAT_PROVIDER: str | None = None  # e.g. "openai"
CHAT_MODEL: str | None = None  # e.g. "openai/gpt-4o-mini"
CHAT_API_KEY: str | None = None
CHAT_API_BASE: str | None = None
CHAT_BACKEND: str | None = None  # "direct_sdk" | "litellm_router" | "litellm_proxy"

SYSTEM_PROMPT = "Return a brief plain-text answer."
USER_PROMPT = "Reply with: harbor-chat-smoke-ok"


async def _run() -> None:
    async with ChatClientFactory.create_async(chat_config()) as client:
        response = await client.achat(
            [
                HarborChatMessage.system(SYSTEM_PROMPT),
                HarborChatMessage.user(USER_PROMPT),
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
    set_env_overrides(
        {
            "HARBOR_CHAT_PROVIDER": CHAT_PROVIDER,
            "HARBOR_CHAT_MODEL": CHAT_MODEL,
            "HARBOR_CHAT_API_KEY": CHAT_API_KEY,
            "HARBOR_CHAT_API_BASE": CHAT_API_BASE,
            "HARBOR_CHAT_BACKEND": CHAT_BACKEND,
        }
    )
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
