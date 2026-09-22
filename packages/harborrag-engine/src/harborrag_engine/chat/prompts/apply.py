"""Provider-independent application of a selected chat prompt."""

from __future__ import annotations

from harborrag_core.models.chat import HarborChatMessage, HarborChatRequest

from .catalog import ChatPrompt, PromptCatalog


def apply_prompt(
    request: HarborChatRequest,
    prompt: ChatPrompt | None,
    catalog: PromptCatalog,
) -> HarborChatRequest:
    if prompt is None:
        return request
    system_message = HarborChatMessage.system(catalog.resolve(prompt))
    return request.model_copy(update={"messages": (system_message, *request.messages)})
