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
from harborrag_engine.tools.catalog import build_reader_tool_catalog
from harborrag_engine.tools.dispatcher import ToolInvoker
from harborrag_engine.tools.references import KnowledgeReferenceStore
from harborrag_mcp_server.audit import McpAuditLog
from harborrag_mcp_server.policy import McpToolPolicy
from harborrag_mcp_server.server.access_gate import TenantListing, require_listed_off_loop
from harborrag_mcp_server.server.base import BaseMcpServer, declares_tenant, tool_reported_error

if TYPE_CHECKING:
    from harborrag_core.ports.reader import ReaderServices
    from harborrag_mcp_server.configuration import McpConfigurationStore

# Shared, process-wide default policy/audit singletons. The module-level
# call_tool/list_tools facade constructs a fresh McpServer per invocation, so
# these live outside the dataclass defaults to retain one audit trail and
# policy across facade calls.
_default_policy = McpToolPolicy()
# The tenant a tool call is bound to when neither a token nor the call names one.
_LOCAL_TENANT = "local"
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
    # The access file's listing, when one is in force: an unlisted principal is refused
    # before any tool runs. None means no access file, and nothing is checked.
    access_watcher: TenantListing | None = None

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

    async def _require_listed(
        self,
        tool: BaseTool | None,
        principal_id: str,
        tenant_id: str | None,
        context: ToolInvocationContext | None,
    ) -> None:
        """Hold a call that arrives without a context to the tenant it will run in.

        An authenticated call carries a context whose tenant was already checked against
        the file. Otherwise the call runs in the tenant it names, or in ``local`` when it
        names none, and the principal must be listed there. Only a tool whose schema
        declares no tenant (``describe_graph``) reads none, and for it listing anywhere
        is enough.
        """

        if context is not None:
            return
        scoped = tool is None or declares_tenant(tool.spec.input_schema)
        bound = (tenant_id or _LOCAL_TENANT) if scoped else None
        await require_listed_off_loop(self.access_watcher, principal_id, bound)

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
                raise PermissionError("reader tool tenant does not match authenticated context")
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
            if self.tools is None:
                raise HarborInvariantError("self.tools must not be None here")
            tool = next((item for item in self.tools if item.spec.name == name), None)
            # Before anything that depends on the tenant, such as whether the tool is
            # enabled for it: an unlisted principal learns nothing about the tenant.
            await self._require_listed(tool, principal_id, tenant_id, context)
            if tool is None:
                raise ValueError(f"Unknown MCP tool: {name}")
            policy = self.policy
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
                        tenant_id=TenantId(tenant_id or _LOCAL_TENANT),
                        corpus_mode=(
                            self.corpus_mode if tenant_id == self.shared_tenant_id else "source_acl"
                        ),
                    ),
                )
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
