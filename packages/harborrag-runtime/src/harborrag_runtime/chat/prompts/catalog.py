"""Typed access to packaged chat prompt templates."""

from __future__ import annotations

from enum import StrEnum
from importlib.resources import files
from typing import Protocol


class ChatPrompt(StrEnum):
    """Stable names for server-owned prompt templates."""

    DEFAULT = "default"
    CONCISE = "concise"


class PromptReader(Protocol):
    def __call__(self, filename: str) -> str: ...


class PromptCatalog:
    """Resolve typed prompt names without exposing filesystem paths."""

    _FILENAMES = {
        ChatPrompt.DEFAULT: "default.md",
        ChatPrompt.CONCISE: "concise.md",
    }

    def __init__(self, reader: PromptReader) -> None:
        self._reader = reader

    @classmethod
    def packaged(cls) -> PromptCatalog:
        templates = files("harborrag_runtime.chat.prompts.templates")
        return cls(lambda filename: templates.joinpath(filename).read_text(encoding="utf-8"))

    def resolve(self, prompt: ChatPrompt) -> str:
        """Load one non-empty prompt and normalize trailing whitespace."""

        content = self._reader(self._FILENAMES[prompt]).strip()
        if not content:
            raise ValueError(f"chat prompt {prompt.value!r} is empty")
        return content
