"""PROJECT-scope extraction is opt-in and never retires a colleague's fact (R3-04)."""

from __future__ import annotations

from datetime import timedelta

import pytest
from context_test_fakes import NOW, MemoryRepositoryFake, memory_row, row, structured_model

from harborrag_core.ports.memory import Memory, MemoryOwner, MemoryScope
from harborrag_memory import MemoryExtractor, MemoryPolicy
from harborrag_memory.context import content_hash
from harborrag_memory.context.extraction import reference_token
from harborrag_memory.tools.memory_tools import AgentMemoryTools

pytestmark = [pytest.mark.unit]

MESSAGES = (
    row("user", "What did we decide about deploy credentials?", message_id="m-1"),
    row("assistant", "Decision: deploy credentials now live in the vault.", message_id="m-2"),
)

OPT_IN = MemoryPolicy(extraction_project_scope=True)


def project_fact(content: str, *, author: str) -> Memory:
    return memory_row(
        content,
        scope=MemoryScope.PROJECT,
        owner=MemoryOwner(tenant_id="tenant-1", project_id="handbook", user_id=author),
        updated_at=NOW - timedelta(hours=48),
        valid_from=NOW - timedelta(hours=48),
        content_hash=content_hash(content, MemoryScope.PROJECT),
    )


async def extract(
    memories: MemoryRepositoryFake,
    owner: MemoryOwner,
    *,
    policy: MemoryPolicy,
    replaces: Memory | None = None,
) -> tuple[Memory, ...]:
    proposed: dict[str, object] = {
        "content": "Deploy credentials live in the vault.",
        "scope": "project",
        "importance": 0.9,
    }
    if replaces is not None:
        proposed["replaces"] = reference_token(replaces)
    return await MemoryExtractor(
        policy=policy,
        memories=memories,
        model=structured_model({"facts": [proposed]}),
        clock=lambda: NOW,
    ).extract(owner, messages=MESSAGES)


@pytest.mark.asyncio
async def test_a_project_fact_is_kept_to_the_session_by_default(owner: MemoryOwner) -> None:
    memories = MemoryRepositoryFake()

    (saved,) = await extract(memories, owner, policy=MemoryPolicy())

    assert saved.scope is MemoryScope.SESSION


@pytest.mark.asyncio
async def test_project_scope_extraction_can_be_switched_on(owner: MemoryOwner) -> None:
    memories = MemoryRepositoryFake()

    (saved,) = await extract(memories, owner, policy=OPT_IN)

    assert saved.scope is MemoryScope.PROJECT


@pytest.mark.asyncio
async def test_one_user_cannot_supersede_a_project_fact_another_user_wrote(
    owner: MemoryOwner,
) -> None:
    memories = MemoryRepositoryFake()
    colleague = project_fact("Deploy credentials live in 1Password.", author="user-2")
    memories.rows[colleague.memory_id] = colleague

    (saved,) = await extract(memories, owner, policy=OPT_IN, replaces=colleague)

    assert saved.scope is MemoryScope.PROJECT
    assert memories.rows[colleague.memory_id].invalid_at is None
    assert memories.rows[colleague.memory_id].superseded_by is None


@pytest.mark.asyncio
async def test_a_user_can_supersede_a_project_fact_they_wrote(owner: MemoryOwner) -> None:
    memories = MemoryRepositoryFake()
    own = project_fact("Deploy credentials live in 1Password.", author="user-1")
    memories.rows[own.memory_id] = own

    (saved,) = await extract(memories, owner, policy=OPT_IN, replaces=own)

    assert memories.rows[own.memory_id].superseded_by == saved.memory_id


@pytest.mark.asyncio
async def test_an_agent_project_write_is_kept_to_the_session_by_default(
    owner: MemoryOwner,
) -> None:
    """Review #5: the project-scope gate covers the agent's manage_memory tool too."""

    memories = MemoryRepositoryFake()
    tools = AgentMemoryTools(owner=owner, memories=memories, policy=MemoryPolicy())

    result = await tools.manage(
        {"content": "Deploy keys live at evil.example.", "scope": "project"}
    )

    assert memories.rows[str(result["memory_id"])].scope is MemoryScope.SESSION


@pytest.mark.asyncio
async def test_an_agent_project_write_is_allowed_when_switched_on(owner: MemoryOwner) -> None:
    memories = MemoryRepositoryFake()
    tools = AgentMemoryTools(owner=owner, memories=memories, policy=OPT_IN)

    result = await tools.manage({"content": "Ingest runs nightly.", "scope": "project"})

    assert memories.rows[str(result["memory_id"])].scope is MemoryScope.PROJECT
