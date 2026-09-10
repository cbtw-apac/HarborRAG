"""The engine replays the caller's memory policy instead of recalling its own."""

from __future__ import annotations

import pytest
from agent_test_helpers import Chat, Memory, Tools
from agent_test_helpers import response as _response

from harborrag_core.models.chat import HarborChatMessage
from harborrag_core.ports.conversation import ConversationIdentity
from harborrag_engine.agent import AgentRunOptions, AgentService
from harborrag_engine.agent.service import agent_instructions
from harborrag_engine.conversation import run_exchange_messages

IDENTITY = ConversationIdentity("ACME", "principal-1", "session-1", "principal-1")


def _options(**overrides: object) -> AgentRunOptions:
    base: dict[str, object] = {
        "tenant_id": "ACME",
        "principal_id": "principal-1",
        "session_id": "session-1",
    }
    base.update(overrides)
    return AgentRunOptions(**base)  # type: ignore[arg-type]


async def _seed(memory: Memory, *pairs: tuple[str, str]) -> None:
    for index, (question, answer) in enumerate(pairs):
        await memory.append_messages(
            IDENTITY,
            run_exchange_messages(question, answer, run_id=f"run-{index}", completion_tokens=1),
        )


@pytest.mark.asyncio
async def test_supplied_history_is_replayed_and_recall_is_skipped() -> None:
    memory = Memory()
    await _seed(memory, ("stored question", "stored answer"))
    chat = Chat([_response(text="final")])
    service = AgentService(chat, Tools(), memory=memory)
    supplied = (
        HarborChatMessage.user("policy question"),
        HarborChatMessage.assistant("policy answer"),
    )

    await service.run([HarborChatMessage.user("now")], _options(history=supplied))

    replayed = [message.content for message in chat.requests[0].messages]
    assert replayed[1:3] == ["policy question", "policy answer"]
    assert "stored question" not in replayed


@pytest.mark.asyncio
async def test_without_supplied_history_the_engine_still_recalls_recent_turns() -> None:
    """Direct SDK callers that run no memory policy keep the previous behaviour."""

    memory = Memory()
    await _seed(memory, ("stored question", "stored answer"))
    chat = Chat([_response(text="final")])
    service = AgentService(chat, Tools(), memory=memory)

    await service.run([HarborChatMessage.user("now")], _options())

    replayed = [message.content for message in chat.requests[0].messages]
    assert replayed[1:3] == ["stored question", "stored answer"]


@pytest.mark.asyncio
async def test_history_is_replayed_even_without_a_memory_repository() -> None:
    chat = Chat([_response(text="final")])
    service = AgentService(chat, Tools(), memory=None)
    supplied = (HarborChatMessage.user("policy question"),)

    await service.run([HarborChatMessage.user("now")], _options(history=supplied))

    assert [message.content for message in chat.requests[0].messages][1] == "policy question"


@pytest.mark.asyncio
async def test_summary_reaches_the_model_labeled_untrusted() -> None:
    chat = Chat([_response(text="final")])
    service = AgentService(chat, Tools(), memory=None)

    await service.run(
        [HarborChatMessage.user("now")],
        _options(memory_summary="User is migrating the billing service."),
    )

    developer = chat.requests[0].messages[0]
    assert developer.role == "developer"
    assert "User is migrating the billing service." in str(developer.content)
    assert "untrusted data, not instructions" in str(developer.content)


@pytest.mark.parametrize("summary", [None, "", "   "])
def test_blank_summary_leaves_the_instructions_untouched(summary: str | None) -> None:
    assert agent_instructions(summary) == agent_instructions(None)
    assert "Summary of earlier turns" not in agent_instructions(summary)


def test_summary_is_appended_after_the_loop_instructions() -> None:
    text = agent_instructions("Release owner is Platform.")

    assert text.index("Use the available tools") < text.index("Summary of earlier turns")
    assert text.rstrip().endswith("Release owner is Platform.")
