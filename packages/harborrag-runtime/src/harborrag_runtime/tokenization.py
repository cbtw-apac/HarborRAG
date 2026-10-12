"""Stable provider-neutral token estimates for ingestion policy."""

from __future__ import annotations

import math

from harborrag_core.contracts.chunking import TokenCounter


class ApproximateTokenCounter:
    """Estimate BPE-sized text while conservatively handling non-ASCII input.

    English and source text average roughly four ASCII characters per token.
    Non-ASCII characters count individually because CJK, emoji, and other
    scripts frequently consume one or more model tokens per character.
    """

    name = "ascii-four-nonascii-one"
    version = "1"

    def count(self, text: str) -> int:
        if not text:
            return 0
        ascii_characters = sum(ord(character) < 128 for character in text)
        non_ascii_characters = len(text) - ascii_characters
        return max(
            1,
            math.ceil(ascii_characters / 4) + non_ascii_characters,
        )


def chunking_token_counter(name: str = "approximate") -> TokenCounter:
    """Build the token counter named by ``RuntimeSettings.chunking_tokenizer``."""

    if name == "approximate":
        return ApproximateTokenCounter()
    if name.startswith("tiktoken:"):
        from harborrag_adapters.chunking import TiktokenTokenCounter

        return TiktokenTokenCounter(name.removeprefix("tiktoken:"))
    raise ValueError(f"unknown chunking tokenizer: {name!r}")


def chunking_tokenizer_identity(name: str = "approximate") -> dict[str, str]:
    """Identify a chunking tokenizer setting without loading the tokenizer.

    Processing identity is computed by API and worker processes alike, and the
    API need not have tiktoken installed, so identity comes from the setting.
    """

    if name == "approximate":
        return {"name": ApproximateTokenCounter.name, "version": ApproximateTokenCounter.version}
    if name.startswith("tiktoken:") and name.removeprefix("tiktoken:"):
        return {"name": "tiktoken", "version": name.removeprefix("tiktoken:")}
    raise ValueError(f"unknown chunking tokenizer: {name!r}")
