from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from harborrag_core.contracts.tools import BaseTool, ToolInvocationContext, ToolSpec
from harborrag_core.invariants import HarborInvariantError
from harborrag_core.schemas.ids import TenantId
from harborrag_core.security import AccessContext
from harborrag_engine.tools.budgets import ToolBudget
from harborrag_engine.tools.catalog import build_reader_tool_catalog
from harborrag_engine.tools.dispatcher import ToolInvoker
from harborrag_engine.tools.references import KnowledgeReferenceStore
from harborrag_mcp_server.audit import McpAuditLog
from harborrag_mcp_server.policy import McpToolPolicy
from harborrag_mcp_server.server.base import BaseMcpServer, tool_reported_error

if TYPE_CHECKING:
    from harborrag_core.ports.reader import ReaderServices
    from harborrag_mcp_server.configuration import McpConfigurationStore

# Shared, process-wide default policy/audit singletons. The module-level
# call_tool/list_tools facade constructs a fresh McpServer per invocation, so
# these live outside the dataclass defaults to retain one audit trail and
# policy across facade calls.
_default_policy = McpToolPolicy()
# Set on a refusal raised before the tool runs. The exception keeps its type, so
# the audit record and the HTTP status mapping are unchanged.
_CALLER_CORRECTABLE = "_harborrag_caller_correctable"
_default_audit_log: McpAuditLog | None = None


def _shared_audit_log() -> McpAuditLog:
    """Create the process audit trail on first use, not at import.

    The CLI imports this module before it loads ``--env-file`` and
    ``--local-stack-root`` values, so reading the path at import ignored an
    ``HARBORRAG_MCP_AUDIT_PATH`` set in those files.
    """

    global _default_audit_log  # noqa: PLW0603 - one process-wide trail by design
    if _default_audit_log is None:
        _default_audit_log = McpAuditLog(
            path=Path(os.environ.get("HARBORRAG_MCP_AUDIT_PATH", ".harborrag/mcp-audit.jsonl"))
        )
    return _default_audit_log


@dataclass(slots=True)
class McpServer(BaseMcpServer):
    """In-process MCP transport enforcing policy and audit boundaries."""

    runtime: ReaderServices | None = None
    tools: list[BaseTool] | None = None
    policy: McpToolPolicy = field(default_factory=lambda: _default_policy)
    audit: McpAuditLog = field(default_factory=_shared_audit_log)
    configuration: McpConfigurationStore | None = None
    references: KnowledgeReferenceStore = field(default_factory=KnowledgeReferenceStore)
    invoker: ToolInvoker | None = None
    corpus_mode: Literal["source_acl", "tenant_shared"] = "source_acl"
    shared_tenant_id: str | None = None

    def __post_init__(self) -> None:
        if self.corpus_mode == "tenant_shared" and not self.shared_tenant_id:
            raise ValueError("shared corpus mode requires an explicit tenant")
        if (
            self.invoker is not None
            and self.tools is not None
            and self.tools is not self.invoker.tools
        ):
            raise HarborInvariantError("MCP discovery and invocation must share one tool catalog")
        if self.tools is None and self.invoker is not None:
            self.tools = self.invoker.tools
        if self.tools is None:
            self.tools = build_reader_tool_catalog(self.runtime, self.references)
        if self.invoker is None:
            self.invoker = ToolInvoker(self.tools)
        missing_output_schema = [
            tool.spec.name for tool in self.tools if tool.spec.output_schema is None
        ]
        if missing_output_schema:
            raise HarborInvariantError(
                "Every registered MCP tool must declare an output_schema; missing for: "
                f"{', '.join(sorted(missing_output_schema))}"
            )

    def list_tools(self, tenant_id: str | None = None) -> list[ToolSpec]:
        if self.tools is None:
            raise HarborInvariantError("self.tools must not be None here")
        if self.configuration is None:
            return [tool.spec for tool in self.tools]
        return [
            self.configuration.tool_spec(tool.spec, tenant_id)
            for tool in self.tools
            if self.configuration.resolve(tool.spec.name, tenant_id).enabled
        ]

    async def call_tool(  # noqa: C901 - audit and policy checks share one boundary
        self,
        name: str,
        arguments: dict[str, object] | None = None,
        *,
        principal_id: str = "in-process",
        context: ToolInvocationContext | None = None,
    ) -> dict[str, object]:
        payload = dict(arguments or {})
        tenant_value = payload.get("tenant_id")
        if isinstance(tenant_value, str):
            # One canonical tenant value must drive configuration, audit, schema
            # validation, and the eventual AccessContext. Otherwise whitespace can
            # select global policy here and a tenant override in the tool layer.
            payload["tenant_id"] = tenant_value.strip()
        audited_tenant = payload.get("tenant_id")
        tenant_id = audited_tenant if isinstance(audited_tenant, str) else None
        if context is not None:
            principal_id = context.access.principal_id
            if tenant_id is not None and tenant_id != str(context.access.tenant_id):
                refusal = PermissionError("reader tool tenant does not match authenticated context")
                setattr(refusal, _CALLER_CORRECTABLE, True)
                raise refusal
            tenant_id = str(context.access.tenant_id)
        # Off the event loop: each durable audit event opens, writes and
        # fsyncs under a lock, twice per call, and this dispatch runs inline in
        # the transport's loop.
        invocation_id = await asyncio.to_thread(
            self.audit.start,
            name,
            payload,
            principal_id=principal_id,
            tenant_id=tenant_id,
        )
        try:
            try:
                spec, policy, defaults, context = self._admit(
                    name, payload, principal_id, tenant_id, context
                )
            except (ValueError, TypeError, PermissionError) as exc:
                setattr(exc, _CALLER_CORRECTABLE, True)
                raise
            context = ToolInvocationContext(access=context.access, invocation_id=invocation_id)
            invoker = self.invoker
            if invoker is None:
                raise HarborInvariantError("MCP tool invoker is not configured")
            result = await invoker.invoke(
                name, payload, context=context, budget=policy, spec=spec, defaults=defaults
            )
            reported_error = tool_reported_error(result)
            await asyncio.to_thread(
                self.audit.finish,
                invocation_id,
                name,
                principal_id=principal_id,
                outcome="error" if reported_error else "success",
                error_type="ToolReportedError" if reported_error else None,
                tenant_id=tenant_id,
            )
            return result
        except BaseException as exc:
            self.audit.finish(
                invocation_id,
                name,
                principal_id=principal_id,
                outcome="error",
                error_type=type(exc).__name__,
                tenant_id=tenant_id,
            )
            raise

    def _admit(
        self,
        name: str,
        payload: dict[str, object],
        principal_id: str,
        tenant_id: str | None,
        context: ToolInvocationContext | None,
    ) -> tuple[ToolSpec, ToolBudget, dict[str, object] | None, ToolInvocationContext]:
        """Run every check that precedes the tool's own execution.

        These refusals -- unknown or disabled tool, a tenant the access policy
        denies, oversized or off-schema arguments -- are the caller's to correct,
        so ``call_tool`` marks them and the transport reports them in full.
        The invoker repeats the same checks on the same inputs, so they cannot
        disagree; anything raised after this point comes from the tool itself
        and stays masked.
        """

        if self.tools is None:
            raise HarborInvariantError("self.tools must not be None here")
        tool = next((item for item in self.tools if item.spec.name == name), None)
        if tool is None:
            raise ValueError(f"Unknown MCP tool: {name}")
        policy: ToolBudget = self.policy
        spec = tool.spec
        defaults: dict[str, object] | None = None
        if self.configuration is not None:
            configured = self.configuration.resolve(name, tenant_id)
            if not configured.enabled:
                raise PermissionError(f"MCP tool {name} is disabled")
            defaults = configured.defaults
            spec = self.configuration.tool_spec(spec, tenant_id)
            policy = self.configuration.policy()
        if context is None:
            context = ToolInvocationContext(
                access=AccessContext(
                    principal_id=principal_id,
                    tenant_id=TenantId(tenant_id or "local"),
                    corpus_mode=(
                        self.corpus_mode if tenant_id == self.shared_tenant_id else "source_acl"
                    ),
                ),
            )
        invoker = self.invoker
        if invoker is not None:
            access_policy = invoker.access_policy
            if access_policy is not None and not access_policy.allowed(context, name):
                raise PermissionError(f"tool {name} is disabled")
            values = {**(defaults or {}), **payload}
            if "tenant_id" in spec.input_schema.get("properties", {}) and str(
                context.access.tenant_id
            ) != values.get("tenant_id"):
                raise PermissionError("reader tool tenant does not match authenticated context")
            invoker.budget.narrowed(policy).check_call(spec, values)
        return spec, policy, defaults, context


def is_caller_correctable(exc: BaseException) -> bool:
    """Whether ``call_tool`` refused ``exc`` before the tool itself ran."""

    return getattr(exc, _CALLER_CORRECTABLE, False) is True
