"""Provider-neutral token estimates for per-turn context assembly.

This mirrors ``harborrag_runtime.tokenization.ApproximateTokenCounter`` on
purpose: the memory package may not import the runtime, and a per-turn window
still has to be trimmed to a token budget without a provider tokenizer.
"""

from __future__ import annotations

import math


def approximate_tokens(text: str) -> int:
    """Estimate BPE-sized text while conservatively handling non-ASCII input.

    English and source text average roughly four ASCII characters per token.
    Non-ASCII characters count individually because CJK, emoji, and other
    scripts frequently consume one or more model tokens per character. Empty
    text costs nothing; any non-empty text costs at least one token.
    """

    if not text:
        return 0
    ascii_characters = sum(ord(character) < 128 for character in text)
    non_ascii_characters = len(text) - ascii_characters
    return max(1, math.ceil(ascii_characters / 4) + non_ascii_characters)


__all__ = ["approximate_tokens"]
