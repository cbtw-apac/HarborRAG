"""Small model boundary for memory policies, with LangChain compatibility."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

from pydantic import BaseModel

if TYPE_CHECKING:
    from langchain_core.language_models import BaseChatModel


@runtime_checkable
class MemoryModel(Protocol):
    """Model operations needed by summaries, query rewrites, and extraction.

    Implementations may raise ``NotImplementedError`` for structured output;
    policies then use their existing plain-text fallback.
    """

    async def generate_text(self, *, system: str, user: str) -> str: ...

    async def generate_structured(
        self, *, schema: type[BaseModel], system: str, user: str
    ) -> object: ...


type MemoryModelLike = MemoryModel | BaseChatModel
