"""Tests for the agent's conversation-memory tools and their owner binding."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from harborrag_core.ports.memory import (
    Memory,
    MemoryOwner,
    MemoryQuery,
    MemoryScope,
    MemoryType,
    visible_to,
)
from harborrag_engine.agent.execution import ChatAndToolExecutor
from harborrag_runtime.agent.memory_tool_specs import manage_memory_schema, search_memory_schema
from harborrag_runtime.agent.tools import RuntimeAgentToolProvider
from harborrag_runtime.config.settings import RuntimeSettings
from harborrag_runtime.memory import RuntimeMemoryContextService

OWNER = MemoryOwner(
    tenant_id="ACME",
    project_id="atlas",
    principal_id="svc-1",
    user_id="dev",
    session_id="session-1",
)
OTHER = MemoryOwner(tenant_id="ACME", principal_id="svc-2", user_id="intruder")


class _Memories:
    """An in-memory repository applying the production visibility rule."""

    def __init__(self) -> None:
        self.rows: dict[str, Memory] = {}

    async def save(self, memory: Memory) -> None:
        self.rows[memory.memory_id] = memory

    async def get(self, caller: MemoryOwner, memory_id: str) -> Memory | None:
        memory = self.rows.get(memory_id)
        if memory is None or not visible_to(memory.scope, memory.owner, caller):
            return None
        return memory

    async def search(self, query: MemoryQuery) -> tuple[Memory, ...]:
        scopes = query.scopes or tuple(MemoryScope)
        found = [
            memory
            for memory in self.rows.values()
            if memory.scope in scopes
            and visible_to(memory.scope, memory.owner, query.owner)
            and (query.text is None or query.text.casefold() in memory.content.casefold())
        ]
        return tuple(found[: query.limit])

    async def delete(self, caller: MemoryOwner, memory_id: str) -> None:
        del caller, memory_id


@dataclass
class _Runtime:
    memory_service: RuntimeMemoryContextService

    def _memory_context_service(self) -> RuntimeMemoryContextService:
        return self.memory_service


async def _no_embedder(settings: RuntimeSettings) -> None:
    del settings
    return None


def _provider(
    memories: _Memories | None = None,
    *,
    enabled: bool = True,
    owner: MemoryOwner | None = OWNER,
    settings: RuntimeSettings | None = None,
) -> RuntimeAgentToolProvider:
    service = RuntimeMemoryContextService(
        settings or RuntimeSettings(),
        embedder_builder=_no_embedder,
    )
    return RuntimeAgentToolProvider(
        _Runtime(service),  # type: ignore[arg-type]
        memories=memories,
        memory_owner=owner,
        memory_tools_enabled=enabled,
    )


def _stored(memory_id: str, content: str, *, scope: MemoryScope = MemoryScope.USER) -> Memory:
    return Memory(
        memory_id=memory_id,
        scope=scope,
        memory_type=MemoryType.PREFERENCE,
        owner=OWNER,
        content=content,
        entity_ids=("node-atlas",),
        source_session_id="session-0",
    )


def test_the_memory_tools_are_not_advertised_by_default() -> None:
    names = {spec.name for spec in _provider(_Memories(), enabled=False).list_tools("ACME")}

    assert "search_memory" not in names
    assert "manage_memory" not in names


def test_the_memory_tools_are_not_advertised_without_a_bound_owner() -> None:
    names = {spec.name for spec in _provider(_Memories(), owner=None).list_tools("ACME")}

    assert "search_memory" not in names


def test_enabled_tools_are_advertised_and_the_write_tool_stays_out_of_the_read_set() -> None:
    provider = _provider(_Memories())
    executor = ChatAndToolExecutor(object(), provider, memory=None)  # type: ignore[arg-type]

    advertised = {spec.name for spec in provider.list_tools("ACME")}
    offered = {spec.name for spec in executor.available_specs("ACME", graph_search=False)}

    assert {"search_memory", "manage_memory"} <= advertised
    # ``search_memory`` is a read tool and is not graph-prefixed, so it survives
    # the engine filter even with graph search off; ``manage_memory`` declares
    # ``write`` and is excluded, which is what makes it inert today.
    assert "search_memory" in offered
    assert "manage_memory" not in offered


@pytest.mark.asyncio
async def test_search_memory_returns_the_recalled_shape_for_the_bound_owner() -> None:
    memories = _Memories()
    await memories.save(_stored("mem-1", "prefers metric units"))
    await memories.save(_stored("mem-2", "hates metric units", scope=MemoryScope.SESSION))

    response = await _provider(memories).call_tool("search_memory", {"query": "metric"})

    assert response["ok"] is True
    recalled = response["memories"]
    assert isinstance(recalled, list) and recalled
    assert set(recalled[0]) == {
        "memory_id",
        "scope",
        "memory_type",
        "content",
        "valid_from",
        "source_session_id",
        "entity_ids",
    }
    assert recalled[0]["entity_ids"] == ["node-atlas"]
    assert recalled[0]["valid_from"] is not None


@pytest.mark.asyncio
async def test_search_memory_narrows_to_one_requested_scope() -> None:
    memories = _Memories()
    await memories.save(_stored("mem-1", "metric units", scope=MemoryScope.USER))
    await memories.save(_stored("mem-2", "metric tonnes", scope=MemoryScope.SESSION))

    response = await _provider(memories).call_tool(
        "search_memory",
        {"query": "metric", "scope": "session"},
    )

    assert [item["memory_id"] for item in response["memories"]] == ["mem-2"]  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_a_memory_tool_is_unavailable_while_the_setting_is_off() -> None:
    provider = _provider(_Memories(), enabled=False)

    search = await provider.call_tool("search_memory", {"query": "metric"})
    manage = await provider.call_tool("manage_memory", {"content": "a fact"})

    # Indistinguishable from an unknown tool, so the flag is not probeable.
    assert search == {"ok": False, "error": "agent tool is not available"}
    assert manage == {"ok": False, "error": "agent tool is not available"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field",
    ["tenant_id", "user_id", "session_id", "principal_id", "project_id", "run_id", "owner"],
)
async def test_owner_fields_supplied_by_the_model_are_rejected(field: str) -> None:
    provider = _provider(_Memories())

    search = await provider.call_tool("search_memory", {"query": "metric", field: "elsewhere"})
    manage = await provider.call_tool("manage_memory", {"content": "a fact", field: "elsewhere"})

    assert search == {
        "ok": False,
        "error": f"{field} is bound by the server and must not be supplied",
    }
    assert manage == {
        "ok": False,
        "error": f"{field} is bound by the server and must not be supplied",
    }


@pytest.mark.asyncio
async def test_search_memory_cannot_see_another_callers_memories() -> None:
    memories = _Memories()
    await memories.save(_stored("mem-1", "metric units"))
    provider = _provider(memories, owner=OTHER)

    response = await provider.call_tool("search_memory", {"query": "metric"})

    assert response["memories"] == []


@pytest.mark.asyncio
async def test_manage_memory_records_for_the_bound_owner_and_is_add_only() -> None:
    memories = _Memories()
    provider = _provider(memories)

    first = await provider.call_tool(
        "manage_memory",
        {"content": "ships on Fridays", "memory_type": "decision", "scope": "project"},
    )
    again = await provider.call_tool(
        "manage_memory",
        {"content": "ships on Fridays", "scope": "project"},
    )

    assert first["ok"] is True and first["recorded"] is True
    stored = memories.rows[str(first["memory_id"])]
    assert stored.owner == OWNER
    assert stored.scope is MemoryScope.PROJECT
    assert stored.memory_type is MemoryType.DECISION
    assert stored.source_session_id == "session-1"
    # The same content in the same scope is found and not duplicated. A different
    # scope is a different statement, so dedup is deliberately per scope.
    assert again == {"ok": True, "memory_id": first["memory_id"], "recorded": False}
    assert len(memories.rows) == 1


@pytest.mark.asyncio
async def test_manage_memory_narrows_a_scope_the_caller_cannot_address() -> None:
    memories = _Memories()
    # No project on this owner, so a project-wide fact must not become tenant-wide.
    owner = MemoryOwner(
        tenant_id="ACME",
        principal_id="svc-1",
        user_id="dev",
        session_id="session-1",
    )

    response = await _provider(memories, owner=owner).call_tool(
        "manage_memory",
        {"content": "uses UTC", "scope": "project"},
    )

    assert memories.rows[str(response["memory_id"])].scope is MemoryScope.USER


@pytest.mark.asyncio
async def test_manage_memory_rejects_an_out_of_range_importance() -> None:
    response = await _provider(_Memories()).call_tool(
        "manage_memory",
        {"content": "uses UTC", "importance": 4},
    )

    assert response == {"ok": False, "error": "importance must be between 0 and 1"}


def test_tenant_scope_is_readable_but_not_writable() -> None:
    """An agent may recall an organization-wide fact; it may not state one."""

    read_scopes = search_memory_schema()["properties"]["scope"]["enum"]  # type: ignore[index]
    write_scopes = manage_memory_schema()["properties"]["scope"]["enum"]  # type: ignore[index]

    assert "tenant" in read_scopes
    assert "tenant" not in write_scopes
    assert set(write_scopes) == {"session", "user", "project"}


@pytest.mark.asyncio
async def test_manage_memory_refuses_a_tenant_wide_write() -> None:
    """Requested scopes only ever narrow, so TENANT must not be requestable at all.

    Every owner can address the tenant scope, so an agent allowed to ask for it
    would always get it -- one user's session stating a fact for the whole
    organization.
    """

    memories = _Memories()

    response = await _provider(memories).call_tool(
        "manage_memory",
        {"content": "the company is metric", "scope": "tenant"},
    )

    assert response == {
        "ok": False,
        "error": "scope must be one of ['project', 'user', 'session']",
    }
    assert memories.rows == {}


@pytest.mark.asyncio
async def test_search_memory_can_recall_a_tenant_wide_fact() -> None:
    memories = _Memories()
    await memories.save(_stored("mem-1", "the company is metric", scope=MemoryScope.TENANT))

    response = await _provider(memories).call_tool(
        "search_memory",
        {"query": "metric", "scope": "tenant"},
    )

    assert [item["memory_id"] for item in response["memories"]] == ["mem-1"]  # type: ignore[union-attr]
