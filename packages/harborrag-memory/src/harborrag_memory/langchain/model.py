"""Compatibility adapter for existing LangChain memory model callers."""

from __future__ import annotations

from dataclasses import dataclass

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel


@dataclass(frozen=True, slots=True)
class LangChainMemoryModel:
    model: BaseChatModel

    async def generate_text(self, *, system: str, user: str) -> str:
        from ..context.prompting import message_text

        response = await self.model.ainvoke(
            [SystemMessage(content=system), HumanMessage(content=user)]
        )
        return message_text(response)

    async def generate_structured(
        self, *, schema: type[BaseModel], system: str, user: str
    ) -> object:
        return await self.model.with_structured_output(schema).ainvoke(
            [SystemMessage(content=system), HumanMessage(content=user)]
        )
