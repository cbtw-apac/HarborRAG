"""The question and answer of one completion are written together with their metadata."""

from __future__ import annotations

import json

import pytest

from harborrag_app.workflow_control.memory import CitationRecord, ExchangeRecord, record_exchange
from harborrag_core.ports.conversation import ConversationIdentity, new_message_id
from harborrag_runtime.memory import InMemoryConversationMemory

pytestmark = pytest.mark.unit

OWNER = ConversationIdentity("ACME", "reader-1", "ses_0a1b", "alice")
CITATION = CitationRecord(
    n=1,
    chunk_id="chk_1",
    document_id="doc_1",
    title="Runbook",
    snippet="Restart the worker.",
    url=None,
    page=3,
)


@pytest.mark.asyncio
async def test_an_exchange_is_stored_as_a_user_and_an_assistant_message() -> None:
    store = InMemoryConversationMemory()
    await store.create(OWNER)

    question, answer = await record_exchange(
        store,
        OWNER,
        ExchangeRecord(
            question="How do I restart?",
            answer="Restart the worker [1].",
            mode="classic",
            trace_id="4bf92f3577b34da6a3ce929d0e0e4736",
            citations=(CITATION,),
            completion_tokens=7,
        ),
    )

    stored = await store.recent_messages(OWNER, limit=10)
    assert stored == (question, answer)
    assert question.role == "user"
    assert question.content == "How do I restart?"
    assert (question.mode, question.trace_id, question.citations_json) == (None, None, None)
    assert answer.role == "assistant"
    assert answer.message_id.startswith("msg_")
    assert question.message_id.startswith("msg_")
    assert answer.mode == "classic"
    assert answer.trace_id == "4bf92f3577b34da6a3ce929d0e0e4736"
    assert answer.token_count == 7
    assert answer.feedback is None
    assert json.loads(answer.citations_json or "[]") == [
        {
            "n": 1,
            "chunk_id": "chk_1",
            "document_id": "doc_1",
            "title": "Runbook",
            "snippet": "Restart the worker.",
            "url": None,
            "page": 3,
        }
    ]


@pytest.mark.asyncio
async def test_a_caller_chosen_answer_id_is_the_stored_message_id() -> None:
    store = InMemoryConversationMemory()
    await store.create(OWNER)
    completion_id = new_message_id()

    _, answer = await record_exchange(
        store,
        OWNER,
        ExchangeRecord(
            question="Q",
            answer="A",
            mode="instant",
            trace_id="0af7651916cd43dd8448eb211c80319c",
            answer_id=completion_id,
        ),
    )

    assert answer.message_id == completion_id
    assert answer.citations_json == "[]"


@pytest.mark.asyncio
async def test_a_store_failure_is_raised_and_nothing_is_kept() -> None:
    store = InMemoryConversationMemory()

    with pytest.raises(ValueError, match="does not exist"):
        await record_exchange(
            store,
            OWNER,
            ExchangeRecord(question="Q", answer="A", mode="classic", trace_id="0" * 32),
        )

    assert await store.recent_messages(OWNER, limit=10) == ()


@pytest.mark.parametrize("mode", ["rag", "advanced", ""])
def test_an_exchange_refuses_a_mode_chat_does_not_answer_in(mode: str) -> None:
    with pytest.raises(ValueError, match="mode"):
        ExchangeRecord(question="Q", answer="A", mode=mode, trace_id="t")  # type: ignore[arg-type]


@pytest.mark.parametrize("n", [0, -1])
def test_a_citation_number_starts_at_one(n: int) -> None:
    with pytest.raises(ValueError, match="n"):
        CitationRecord(n=n, chunk_id="chk_1", document_id="doc_1", title="T", snippet="S")
