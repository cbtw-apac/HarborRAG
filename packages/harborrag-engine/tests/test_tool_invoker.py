"""Transport-independent authorization and validation around reader tools."""

from dataclasses import dataclass

import pytest

from harborrag_core.contracts.reader import RetrievalLane, RetrievalResponse
from harborrag_core.contracts.tools import BaseTool, ToolInvocationContext, ToolSpec
from harborrag_core.security import AccessContext
from harborrag_engine.agent.tools import ReaderAgentToolProvider
from harborrag_engine.tools.budgets import ToolBudget
from harborrag_engine.tools.dispatcher import ToolInvoker
from harborrag_engine.tools.vector_search import VectorSearchTool


@dataclass
class EchoTool(BaseTool):
    spec = ToolSpec(
        "echo_reader",
        "Echo a tenant-scoped reader request.",
        {
            "type": "object",
            "required": ["tenant_id"],
            "properties": {"tenant_id": {"type": "string"}},
            "additionalProperties": False,
        },
        output_schema={
            "type": "object",
            "required": ["ok"],
            "properties": {"ok": {"const": True}},
            "additionalProperties": False,
        },
    )

    async def call(self, arguments: dict[str, object], *, principal_id: str) -> dict[str, object]:
        assert principal_id == "reader"
        return {"ok": True}


@pytest.mark.asyncio
async def test_invoker_enforces_trusted_tenant_and_output_schema() -> None:
    invoker = ToolInvoker([EchoTool()])
    context = ToolInvocationContext(AccessContext(principal_id="reader", tenant_id="acme"))
    assert await invoker.invoke("echo_reader", {"tenant_id": "acme"}, context=context) == {
        "ok": True
    }
    with pytest.raises(PermissionError):
        await invoker.invoke("echo_reader", {"tenant_id": "other"}, context=context)
    with pytest.raises(PermissionError):
        await invoker.invoke("echo_reader", {"tenant_id": "acme"}, context=context, enabled=False)


@pytest.mark.asyncio
async def test_execution_audit_records_rejected_and_completed_calls() -> None:
    class Audit:
        def __init__(self) -> None:
            self.outcomes: list[str] = []

        async def record(self, context: ToolInvocationContext, name: str, outcome: str) -> None:
            self.outcomes.append(outcome)

    audit = Audit()
    invoker = ToolInvoker([EchoTool()], audit=audit)
    context = ToolInvocationContext(AccessContext(principal_id="reader", tenant_id="acme"))

    await invoker.invoke("echo_reader", {"tenant_id": "acme"}, context=context)
    with pytest.raises(PermissionError):
        await invoker.invoke("echo_reader", {"tenant_id": "other"}, context=context)
    assert audit.outcomes == ["success", "error"]


@pytest.mark.asyncio
async def test_caller_budget_cannot_widen_shared_limits() -> None:
    invoker = ToolInvoker([EchoTool()], budget=ToolBudget(max_argument_bytes=10))
    context = ToolInvocationContext(AccessContext(principal_id="reader", tenant_id="acme"))

    with pytest.raises(ValueError, match="argument budget exceeded"):
        await invoker.invoke(
            "echo_reader",
            {"tenant_id": "acme"},
            context=context,
            budget=ToolBudget(label="Agent", max_argument_bytes=1000),
        )


@pytest.mark.asyncio
async def test_agent_provider_binds_reader_calls_to_its_tenant() -> None:
    provider = ReaderAgentToolProvider(ToolInvoker([EchoTool()]), tenant_id="acme")

    assert await provider.call_tool(
        "echo_reader", {"tenant_id": "acme"}, principal_id="reader"
    ) == {"ok": True}
    assert await provider.call_tool(
        "echo_reader", {"tenant_id": "other"}, principal_id="reader"
    ) == {"ok": False, "error": "reader tool tenant does not match authenticated context"}


@pytest.mark.asyncio
async def test_vector_tool_runs_with_only_a_retrieval_reader() -> None:
    class Reader:
        async def search(self, request: object) -> RetrievalResponse:
            return RetrievalResponse("search-1", RetrievalLane.HYBRID, (), {})

    tool = VectorSearchTool(reader=Reader())
    assert tool.runtime is None
    result = await tool.call({"tenant_id": "acme", "query": "policy"}, principal_id="reader")
    assert result["ok"] is True
    assert result["results"] == []
    context = ToolInvocationContext(AccessContext(principal_id="reader", tenant_id="acme"))
    with pytest.raises(PermissionError, match="principal"):
        await tool.call(
            {"tenant_id": "acme", "query": "policy"},
            principal_id="other",
            context=context,
        )
