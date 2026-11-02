"""Exact model token counting for chunk limits"""

from __future__ import annotations

import importlib.metadata
from threading import Lock
from typing import Any


class TiktokenTokenCounter:
    """Implement core ``TokenCounter`` with a tiktoken encoding.

    User text can contain strings such as ``<|endoftext|>`` that tiktoken
    treats as special tokens and refuses by default; they are counted as
    ordinary content here, so a pasted prompt never fails ingestion.
    """

    name = "tiktoken"

    def __init__(self, encoding: str = "cl100k_base") -> None:
        if not encoding.strip():
            raise ValueError("encoding must be non-empty")
        self._encoding_name = encoding.strip()
        self._encoding: Any | None = None
        self._lock = Lock()
        try:
            importlib.metadata.version("tiktoken")
        except importlib.metadata.PackageNotFoundError as exc:
            raise ImportError(
                "TiktokenTokenCounter requires the harborrag-adapters[chunking-tokens] extra"
            ) from exc
        self.version = self._encoding_name

    def count(self, text: str) -> int:
        """Return the number of tokens in ``text``."""

        if not text:
            return 0
        return len(self._get_encoding().encode(text, allowed_special="all"))

    def _get_encoding(self) -> Any:
        if self._encoding is None:
            with self._lock:
                if self._encoding is None:
                    import tiktoken

                    self._encoding = tiktoken.get_encoding(self._encoding_name)
        return self._encoding
