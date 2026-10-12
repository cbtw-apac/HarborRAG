"""User erasure runs every step, journals the outcome, and never claims success it lacks."""

from __future__ import annotations

import asyncio
import re
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from app_test_memory import FakeMemoryIndex, FakeMemoryStore, memory
from chat_service_fixtures import FakeUsageRepository

from harborrag_app.workflow_control.memory import (
    MemoryAdministrationService,
    UserErasureService,
)
from harborrag_app.workflow_control.memory import administration as administration_module
from harborrag_app.workflow_control.memory import user_erasure as user_erasure_module
from harborrag_core.base import utc_now
from harborrag_core.contracts.errors import HarborValidationError
from harborrag_core.domain.identity import DEFAULT_USER
from harborrag_core.models.chat import HarborChatUsage
from harborrag_core.ports.memory import MemoryQuery
from harborrag_core.ports.usage import ModelUsageRecord
from harborrag_core.testing.control_plane_fakes import FakePendingEffectRepository
from harborrag_core.testing.erasure_fakes import FakeUserErasureJournal
from harborrag_runtime.agent import (
    AgentCheckpoint,
    AgentRunIdentity,
    AgentRunStatus,
    InMemoryAgentRunRepository,
)
from harborrag_runtime.memory import ConversationIdentity, InMemoryConversationMemory

_MINE = ConversationIdentity("ACME", "svc", "session-1", "user-1")
_THEIRS = ConversationIdentity("ACME", "svc", "session-9", "user-2")
_PSEUDONYM = re.compile(r"^erased-[0-9a-f]{32}$")


@pytest.fixture
def stores() -> SimpleNamespace:
    conversations = InMemoryConversationMemory()
    memories, index = FakeMemoryStore(), FakeMemoryIndex()
    return SimpleNamespace(
        conversations=conversations,
        memories=memories,
        index=index,
        runs=InMemoryAgentRunRepository(),
        usage=FakeUsageRepository(),
        journal=FakeUserErasureJournal(),
        admin=MemoryAdministrationService(
            conversations=conversations,
            memories=memories,
            index=index,
            pending_effects=FakePendingEffectRepository(),
        ),
    )


def _service(stores: SimpleNamespace) -> UserErasureService:
    return UserErasureService(
        conversations=stores.conversations,
        administration=stores.admin,
        runs=stores.runs,
        usage=stores.usage,
        journal=stores.journal,
    )


def _usage(user_id: str, tenant_id: str = "ACME") -> ModelUsageRecord:
    return ModelUsageRecord(
        tenant_id=tenant_id,
        user_id=user_id,
        principal_id="svc",
        surface="chat",
        logical_model="chat-default",
        provider="openai",
        provider_model="gpt-test",
        prompt_tokens=10,
        completion_tokens=4,
        total_tokens=14,
        session_id="session-1",
    )


def _checkpoint(identity: AgentRunIdentity) -> AgentCheckpoint:
    now = datetime.now(UTC)
    return AgentCheckpoint(
        identity=identity,
        status=AgentRunStatus.RUNNING,
        step=0,
        version=1,
        messages=(),
        executions=(),
        usage=HarborChatUsage(),
        stop_reason=None,
        response=None,
        created_at=now,
        updated_at=now,
    )


async def _seed(stores: SimpleNamespace) -> None:
    for identity in (_MINE, _THEIRS):
        await stores.conversations.create(identity, kind="agent", title="synthetic title")
    await stores.runs.create(
        _checkpoint(AgentRunIdentity("ACME", "svc", "session-1", "run-1", "user-1"))
    )
    claim = {"tenant_id": "ACME", "user_id": "user-1", "key": "k-1", "request_hash": "h"}
    await stores.conversations.claim_completion(**claim)
    await stores.conversations.finish_completion(**claim, response_json='{"text":"answer"}')
    for user_id in ("user-1", "user-1", "user-2"):
        await stores.usage.record(_usage(user_id))
    await stores.memories.save(memory("mem-1", tenant_id="ACME", user_id="user-1"))


@pytest.mark.asyncio
async def test_every_store_is_erased_counted_and_journaled(stores: SimpleNamespace) -> None:
    await _seed(stores)

    report = await _service(stores).erase(tenant_id="ACME", user_id="user-1", actor="admin-1")

    assert report.record.status == "completed"
    assert report.as_dict() == {
        "erasure_id": report.record.erasure_id,
        "status": "completed",
        "sessions": 1,
        "memories": 1,
        "index_points": 1,
        "deferred_index_points": 0,
        "agent_run_checkpoints": 1,
        "completion_requests": 1,
        "model_usage_pseudonymized": 2,
    }
    assert stores.journal.records[report.record.erasure_id] == report.record
    assert not await stores.conversations.exists(_MINE)
    assert await stores.conversations.exists(_THEIRS)
    assert (await stores.usage.totals(tenant_id="ACME")).requests == 3
    assert (await stores.usage.totals(tenant_id="ACME", user_id="user-1")).requests == 0
    replay = await stores.conversations.claim_completion(
        tenant_id="ACME", user_id="user-1", key="k-1", request_hash="h"
    )
    assert replay.result_json is None


@pytest.mark.asyncio
async def test_every_step_runs_even_after_one_fails(
    stores: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _seed(stores)
    monkeypatch.setattr(
        stores.runs, "delete_for_owner", AsyncMock(side_effect=RuntimeError("store offline"))
    )

    report = await _service(stores).erase(tenant_id="ACME", user_id="user-1", actor="admin-1")

    assert report.record.status == "failed"
    assert report.record.failed_steps == ("agent_runs",)
    assert report.record.counts["completion_requests"] == 1
    assert report.record.counts["model_usage_pseudonymized"] == 2
    assert stores.journal.records[report.record.erasure_id].status == "failed"


@pytest.mark.asyncio
async def test_nothing_is_touched_when_the_journal_cannot_record_the_start(
    stores: SimpleNamespace,
) -> None:
    await _seed(stores)
    stores.journal.failure = RuntimeError("journal offline")

    with pytest.raises(RuntimeError, match="journal offline"):
        await _service(stores).erase(tenant_id="ACME", user_id="user-1", actor="admin-1")

    assert await stores.conversations.exists(_MINE)
    assert "mem-1" in stores.memories.rows


@pytest.mark.asyncio
async def test_a_queued_vector_delete_makes_the_erasure_deferred(stores: SimpleNamespace) -> None:
    await stores.memories.save(memory("mem-1", tenant_id="ACME", user_id="user-1"))
    stores.index.failure = RuntimeError("qdrant gone")

    report = await _service(stores).erase(tenant_id="ACME", user_id="user-1", actor="admin-1")

    assert report.record.status == "deferred"
    assert report.record.counts["deferred_index_points"] == 1
    assert stores.memories.rows == {}


@pytest.mark.asyncio
async def test_the_shared_default_user_is_refused_before_anything_is_recorded(
    stores: SimpleNamespace,
) -> None:
    """DEFAULT_USER is the sentinel every user-less caller shares, so it names nobody."""

    identity = ConversationIdentity("ACME", "svc", "session-1", DEFAULT_USER)
    await stores.conversations.create(identity, kind="chat")
    await stores.memories.save(memory("mem-1", tenant_id="ACME", user_id=DEFAULT_USER))
    await stores.usage.record(_usage(DEFAULT_USER))

    with pytest.raises(HarborValidationError, match=DEFAULT_USER):
        await _service(stores).erase(tenant_id="ACME", user_id=DEFAULT_USER, actor="admin-1")

    assert stores.journal.records == {}
    assert await stores.conversations.exists(identity)
    assert "mem-1" in stores.memories.rows
    assert stores.usage.records[0].user_id == DEFAULT_USER


@pytest.mark.asyncio
async def test_an_expired_user_memory_is_erased_through_the_ownership_query(
    stores: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Expired rows are hidden from visibility reads; only ``stored_by_owner`` reaches them."""

    past = utc_now() - timedelta(days=2)
    expired = replace(
        memory("mem-expired", tenant_id="ACME", user_id="user-1", updated_at=past),
        expires_at=past + timedelta(days=1),
    )
    await stores.memories.save(expired)
    queries: list[MemoryQuery] = []
    search = stores.memories.search

    async def spy(query: MemoryQuery):  # noqa: ANN202 - mirrors the wrapped search
        queries.append(query)
        return await search(query)

    monkeypatch.setattr(stores.memories, "search", spy)

    report = await _service(stores).erase(tenant_id="ACME", user_id="user-1", actor="admin-1")

    assert report.record.counts["memories"] == 1
    assert stores.memories.rows == {}
    assert queries
    assert all(query.stored_by_owner and query.scopes == () for query in queries)


@pytest.mark.asyncio
async def test_the_same_user_id_in_another_tenant_is_never_touched(
    stores: SimpleNamespace,
) -> None:
    await _seed(stores)
    other = ConversationIdentity("OTHER", "svc", "session-1", "user-1")
    other_run = AgentRunIdentity("OTHER", "svc", "session-1", "run-other", "user-1")
    await stores.conversations.create(other, kind="agent")
    await stores.runs.create(_checkpoint(other_run))
    claim = {"tenant_id": "OTHER", "user_id": "user-1", "key": "k-1", "request_hash": "h"}
    await stores.conversations.claim_completion(**claim)
    await stores.conversations.finish_completion(**claim, response_json='{"text":"kept"}')
    await stores.usage.record(_usage("user-1", tenant_id="OTHER"))
    await stores.memories.save(memory("mem-other", tenant_id="OTHER", user_id="user-1"))

    report = await _service(stores).erase(tenant_id="ACME", user_id="user-1", actor="admin-1")

    assert report.record.status == "completed"
    assert await stores.conversations.exists(other)
    assert await stores.runs.get(other_run) is not None
    replay = await stores.conversations.claim_completion(**claim)
    assert replay.result_json == '{"text":"kept"}'
    assert (await stores.usage.totals(tenant_id="OTHER", user_id="user-1")).requests == 1
    assert list(stores.memories.rows) == ["mem-other"]


@pytest.mark.asyncio
async def test_usage_is_reattributed_to_a_fresh_random_pseudonym(
    stores: SimpleNamespace,
) -> None:
    await _seed(stores)
    service = _service(stores)

    await service.erase(tenant_id="ACME", user_id="user-1", actor="admin-1")
    await service.erase(tenant_id="ACME", user_id="user-2", actor="admin-1")

    tokens = {record.user_id for record in stores.usage.records}
    assert len(tokens) == 2
    assert all(_PSEUDONYM.match(token) for token in tokens)
    assert not any("user-" in token for token in tokens)


@pytest.mark.asyncio
async def test_a_busy_session_is_kept_while_the_users_other_sessions_go(
    stores: SimpleNamespace,
) -> None:
    busy = ConversationIdentity("ACME", "svc", "session-busy", "user-1")
    for identity in (_MINE, busy):
        await stores.conversations.create(identity, kind="chat")
    # A child task holds the lease, so the erasure meets it as a stranger would.
    assert await asyncio.create_task(
        stores.conversations.acquire_turn_lease(busy, token="turn-1", lease_seconds=60)
    )

    report = await _service(stores).erase(tenant_id="ACME", user_id="user-1", actor="admin-1")

    assert report.record.status == "failed"
    assert report.record.failed_steps == ("sessions",)
    assert report.record.counts["sessions"] == 1
    assert not await stores.conversations.exists(_MINE)
    assert await stores.conversations.exists(busy)


@pytest.mark.asyncio
async def test_sessions_beyond_one_page_are_all_deleted_by_re_listing(
    stores: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two passes of two delete all four; the last pass must not read as a failure."""

    monkeypatch.setattr(user_erasure_module, "MAX_CONVERSATION_PAGE_LIMIT", 2)
    monkeypatch.setattr(user_erasure_module, "_MAX_SESSION_PASSES", 2)
    identities = [
        ConversationIdentity("ACME", "svc", f"session-{index}", "user-1") for index in range(4)
    ]
    for identity in identities:
        await stores.conversations.create(identity, kind="chat")

    report = await _service(stores).erase(tenant_id="ACME", user_id="user-1", actor="admin-1")

    assert report.record.status == "completed"
    assert report.record.counts["sessions"] == 4
    for identity in identities:
        assert not await stores.conversations.exists(identity)


@pytest.mark.asyncio
async def test_sessions_left_after_the_pass_limit_fail_the_step(
    stores: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(user_erasure_module, "MAX_CONVERSATION_PAGE_LIMIT", 1)
    monkeypatch.setattr(user_erasure_module, "_MAX_SESSION_PASSES", 1)
    for index in range(2):
        identity = ConversationIdentity("ACME", "svc", f"session-{index}", "user-1")
        await stores.conversations.create(identity, kind="chat")

    report = await _service(stores).erase(tenant_id="ACME", user_id="user-1", actor="admin-1")

    assert report.record.failed_steps == ("sessions",)
    assert report.record.counts["sessions"] == 1


@pytest.mark.asyncio
async def test_a_memory_purge_that_finishes_on_its_last_pass_is_complete(
    stores: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(administration_module, "_PAGE", 2)
    monkeypatch.setattr(administration_module, "_MAX_PASSES", 1)
    for memory_id in ("mem-1", "mem-2"):
        await stores.memories.save(memory(memory_id, tenant_id="ACME", user_id="user-1"))

    report = await _service(stores).erase(tenant_id="ACME", user_id="user-1", actor="admin-1")

    assert report.record.status == "completed"
    assert report.record.counts["memories"] == 2


@pytest.mark.asyncio
async def test_a_journal_finish_failure_is_logged_and_raised(
    stores: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    await _seed(stores)
    # The fake is a slotted dataclass, so the method is patched on its type.
    monkeypatch.setattr(
        type(stores.journal), "finish", AsyncMock(side_effect=RuntimeError("journal offline"))
    )

    with pytest.raises(RuntimeError, match="journal offline"):
        await _service(stores).erase(tenant_id="ACME", user_id="user-1", actor="admin-1")

    [started] = stores.journal.records.values()
    warning = next(r for r in caplog.records if "could not be journaled" in r.getMessage())
    assert started.erasure_id in warning.getMessage()
    assert "status=completed" in warning.getMessage()
