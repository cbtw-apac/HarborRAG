"""Provider-neutral token estimates for per-turn context assembly.

A per-turn window is trimmed to a token budget with the core approximate counter,
not a provider tokenizer.
"""

from __future__ import annotations

from harborrag_core.chunking.tokenization import ApproximateTokenCounter

_COUNTER = ApproximateTokenCounter()


def approximate_tokens(text: str) -> int:
    """Estimate the tokens of ``text``: 0 when empty, otherwise at least 1."""
    return _COUNTER.count(text)
