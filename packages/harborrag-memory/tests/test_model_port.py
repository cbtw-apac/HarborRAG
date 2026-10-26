import pytest
from pydantic import BaseModel

from harborrag_memory.context.condenser import condense_question
from harborrag_memory.context.proposing import propose_facts
from harborrag_memory.context.summarizer import summarize


class NeutralModel:
    async def generate_text(self, *, system: str, user: str) -> str:
        return "A summary."

    async def generate_structured(
        self, *, schema: type[BaseModel], system: str, user: str
    ) -> object:
        if schema.__name__ == "CondenseRequest":
            return {"query": "Who owns ingestion?", "wanted_types": ["fact"]}
        return {"facts": []}


@pytest.mark.asyncio
async def test_memory_policies_accept_a_framework_neutral_model() -> None:
    model = NeutralModel()
    assert await summarize(model, prior=None, messages=()) == "A summary."
    result = await condense_question(model, question="Who owns it?", summary=None, messages=())
    assert result is not None and result.query == "Who owns ingestion?"
    assert await propose_facts(model, "a transcript") == {"facts": []}
