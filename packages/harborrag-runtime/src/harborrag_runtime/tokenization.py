"""Stable provider-neutral token estimates for ingestion policy."""

from __future__ import annotations

import math


class ApproximateTokenCounter:
    """Estimate BPE-sized text while conservatively handling non-ASCII input.

    English and source text average roughly four ASCII characters per token.
    Non-ASCII characters count individually because CJK, emoji, and other
    scripts frequently consume one or more model tokens per character.
    """

    def count(self, text: str) -> int:
        if not text:
            return 0
        ascii_characters = sum(ord(character) < 128 for character in text)
        non_ascii_characters = len(text) - ascii_characters
        return max(
            1,
            math.ceil(ascii_characters / 4) + non_ascii_characters,
        )
