from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from harborrag_core.models.errors import HarborChatProviderError

from harborrag_adapters.models.common.responses import (
    coerce_sdk_mapping as coerce_mapping,
)


def normalize_reasoning_content(
    choice: Mapping[str, Any], message: Mapping[str, Any]
) -> str | None:
    """Return LiteLLM's provider-neutral reasoning text from a complete response."""

    candidates = (
        message.get("reasoning_content"),
        choice.get("reasoning_content"),
        message.get("reasoning"),
        choice.get("reasoning"),
    )
    for value in candidates:
        normalized = _reasoning_text(value)
        if normalized:
            return normalized
    blocks = _thinking_blocks(choice, message)
    text = "".join(str(block.get("thinking") or "") for block in blocks)
    return text or None


def normalize_reasoning_delta(delta: Mapping[str, Any]) -> str | None:
    """Return one normalized reasoning fragment from a streaming delta."""

    for name in ("reasoning_content", "reasoning"):
        value = delta.get(name)
        if value is None or value == "":
            continue
        normalized = _reasoning_text(value)
        if normalized is None:
            raise HarborChatProviderError(
                "malformed provider stream: reasoning delta must be text",
                operation="chat",
                retryable=False,
            )
        return normalized
    blocks = delta.get("thinking_blocks")
    if isinstance(blocks, Sequence) and not isinstance(blocks, (str, bytes)):
        text = "".join(str(coerce_mapping(block).get("thinking") or "") for block in blocks)
        return text or None
    return None


def reasoning_metadata(choice: Mapping[str, Any], message: Mapping[str, Any]) -> dict[str, Any]:
    """Return non-content reasoning metadata suitable for diagnostics."""

    blocks = _thinking_blocks(choice, message)
    if not blocks:
        return {}
    return {
        "thinking_block_count": len(blocks),
        "thinking_block_types": tuple(str(block.get("type") or "unknown") for block in blocks),
        "thinking_signatures_present": any(bool(block.get("signature")) for block in blocks),
    }


def _reasoning_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        text = value.get("content") or value.get("text") or value.get("reasoning")
        return text if isinstance(text, str) else None
    return None


def _thinking_blocks(
    choice: Mapping[str, Any], message: Mapping[str, Any]
) -> tuple[dict[str, Any], ...]:
    provider_fields = coerce_mapping(message.get("provider_specific_fields"))
    raw = (
        message.get("thinking_blocks")
        or choice.get("thinking_blocks")
        or provider_fields.get("thinking_blocks")
    )
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        return ()
    return tuple(block for item in raw if (block := coerce_mapping(item)))
