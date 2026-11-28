"""In-process MCP tool registry that enforces configuration, policy and audit.

One default policy and one audit trail are shared by every ``McpServer`` in the
process, because the module-level facade builds a new server for each call.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

from harborrag_core.config_store import SettingsLevel
from harborrag_core.contracts.errors import HarborUnavailableError
from harborrag_core.contracts.tools import BaseTool, ToolInvocationContext, ToolSpec
from harborrag_core.invariants import HarborInvariantError
from harborrag_core.schemas.ids import TenantId
from harborrag_core.security import AccessContext
from harborrag_engine.tools.budgets import ToolBudget
from harborrag_engine.tools.catalog import build_reader_tool_catalog
from harborrag_engine.tools.dispatcher import ToolInvoker
from harborrag_engine.tools.references import KnowledgeReferenceStore
from harborrag_mcp_server.audit import DEFAULT_AUDIT_PATH, McpAuditLog
from harborrag_mcp_server.policy import McpToolPolicy
from harborrag_mcp_server.server.base import BaseMcpServer, tool_reported_error

if TYPE_CHECKING:
    from harborrag_core.ports.config_store import ConfigProvider
    from harborrag_core.ports.reader import ReaderServices
    from harborrag_core.ports.tenant_directory import TenantDirectory
    from harborrag_mcp_server.configuration import McpConfigurationStore

logger = logging.getLogger("harborrag.mcp.server")

_TENANT_LOOKUP_TIMEOUT_SECONDS = 2.0
_default_policy = McpToolPolicy()
_CALLER_CORRECTABLE = "_harborrag_caller_correctable"
_default_audit_log: McpAuditLog | None = None
_TOP_K_KEY = "retrieval.top_k"
_VECTOR_SEARCH = "vector_search"


def _shared_audit_log() -> McpAuditLog:
    """Create the default process audit trail on first use, not at import.

    The launched server passes its own ``McpAuditLog``; this default serves
    in-process callers that name none.
    """
    global _default_audit_log  # noqa: PLW0603 - one process-wide trail by design
    if _default_audit_log is None:
        _default_audit_log = McpAuditLog(path=DEFAULT_AUDIT_PATH)
    return _default_audit_log


@dataclass(slots=True)
class McpServer(BaseMcpServer):
    """In-process MCP transport enforcing policy and audit boundaries.

    Attributes:
        runtime: Reader services the default tool catalog is built from.
        tools: The tool catalog. Left unset, it comes from the invoker or the runtime.
        policy: Limits applied when no configuration store is attached.
        audit: The trail that records every tool call.
        configuration: Tool defaults, limits and enablement, global and per tenant.
        references: Knowledge references shared with the tool catalog.
        invoker: Runs the tools. It must share the catalog in ``tools``.
        corpus_mode: Access mode for calls to ``shared_tenant_id``. Other tenants
            use source ACLs.
        shared_tenant_id: The tenant whose published corpus is shared.
        config_provider: Source of the tenant-level ``retrieval.top_k`` override.
        known_tenants: Tenants this deployment was started with.
        tenant_directory: Finds the tenants that hold content. It crosses tenants,
            so only ``tenant_choices`` reads it.
    """

    runtime: ReaderServices | None = None
    tools: list[BaseTool] | None = None
    policy: McpToolPolicy = field(default_factory=lambda: _default_policy)
    audit: McpAuditLog = field(default_factory=_shared_audit_log)
    configuration: McpConfigurationStore | None = None
    references: KnowledgeReferenceStore = field(default_factory=KnowledgeReferenceStore)
    invoker: ToolInvoker | None = None
    corpus_mode: Literal["source_acl", "tenant_shared"] = "source_acl"
    shared_tenant_id: str | None = None
    config_provider: ConfigProvider | None = None
    known_tenants: tuple[str, ...] = ()
    tenant_directory: TenantDirectory | None = None

    def __post_init__(self) -> None:
        """Build the catalog and invoker left unset and reject an inconsistent setup."""
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

    async def tenant_choices(self) -> list[str]:
        """List the tenants to offer a caller who may name any tenant.

        The lookup of tenants that hold content is best effort. When it fails or
        takes too long, the configured tenants are returned alone and a warning
        is logged, so a caller can still choose one.

        Returns:
            The tenants that hold content, the tenants the deployment was started
            with, the shared tenant and the tenants the configuration overrides,
            sorted.
        """
        overridden = self.configuration.snapshot().tenants if self.configuration else {}
        shared = (self.shared_tenant_id,) if self.shared_tenant_id else ()
        with_content = await self._tenants_with_content()
        return sorted({*self.known_tenants, *shared, *overridden, *with_content})

    async def _tenants_with_content(self) -> list[str]:
        """Return the directory's tenants, or none when it cannot be read in time."""
        if self.tenant_directory is None:
            return []
        try:
            async with asyncio.timeout(_TENANT_LOOKUP_TIMEOUT_SECONDS):
                return [str(tenant) for tenant in await self.tenant_directory.tenant_ids()]
        except (HarborUnavailableError, TimeoutError) as exc:
            logger.warning("tenant_directory_unavailable", extra={"error": type(exc).__name__})
            return []

    def list_tools(self, tenant_id: str | None = None) -> list[ToolSpec]:
        """List the specs of the tools enabled for a tenant, with its configured defaults.

        Args:
            tenant_id: The tenant whose overrides apply, or None for the global view.

        Returns:
            The enabled tools' specs.
        """
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
        """Run one tool under configuration, policy and audit.

        The tenant value is stripped once, so configuration, audit, schema
        validation and the access context all see the same tenant. Audit events
        are written off the event loop, because each one is flushed to disk.

        Args:
            name: The tool to run.
            arguments: The tool's arguments. ``tenant_id`` selects the tenant.
            principal_id: Who calls, for the audit trail, when no ``context`` is given.
            context: The authenticated caller. Its tenant must match ``tenant_id``.

        Returns:
            The tool's result object.

        Raises:
            PermissionError: The tool is disabled or the tenant is not the caller's.
            ValueError: The tool is unknown, or the arguments break its schema or budget.
        """
        payload = dict(arguments or {})
        tenant_value = payload.get("tenant_id")
        if isinstance(tenant_value, str):
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
        invocation_id = await asyncio.to_thread(
            self.audit.start,
            name,
            payload,
            principal_id=principal_id,
            tenant_id=tenant_id,
        )
        try:
            top_k_default = await self._tenant_top_k_override(name, tenant_id)
            try:
                spec, policy, defaults, context = self._admit(
                    name, payload, principal_id, tenant_id, context, top_k_default
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

    async def _tenant_top_k_override(self, name: str, tenant_id: str | None) -> int | None:
        """Return the tenant-level ``retrieval.top_k`` override for ``vector_search``.

        Args:
            name: The tool being called. Only ``vector_search`` has the override.
            tenant_id: The tenant whose settings to read.

        Returns:
            The override, or None when the tool, the tenant or the setting has none.

        Raises:
            HarborUnavailableError: The provider cannot load a never-cached tenant.
        """
        if self.config_provider is None or name != _VECTOR_SEARCH or tenant_id is None:
            return None
        effective = await self.config_provider.effective(tenant_id, SettingsLevel("tenant"))
        override = effective.override(_TOP_K_KEY)
        return override if isinstance(override, int) else None

    def _admit(  # noqa: PLR0913 - one argument per input of the pre-execution checks
        self,
        name: str,
        payload: dict[str, object],
        principal_id: str,
        tenant_id: str | None,
        context: ToolInvocationContext | None,
        top_k_default: int | None = None,
    ) -> tuple[ToolSpec, ToolBudget, dict[str, object] | None, ToolInvocationContext]:
        """Run every check that precedes the tool's own execution.

        These refusals -- unknown or disabled tool, a tenant the access policy
        denies, oversized or off-schema arguments -- are the caller's to correct,
        so ``call_tool`` marks them and the transport reports them in full.
        The invoker repeats the same checks on the same inputs, so they cannot
        disagree; anything raised after this point comes from the tool itself
        and stays masked.

        ``top_k_default`` replaces the configured ``top_k`` default, still
        clamped by the tool's ``top_k`` limit and the policy ceiling.
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
            if top_k_default is not None:
                defaults = {**defaults, "top_k": _clamped(top_k_default, policy, configured.limits)}
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


def _clamped(top_k: int, policy: McpToolPolicy, limits: dict[str, int | float]) -> int:
    """Return ``top_k`` under the policy ceiling and the tool's ``top_k`` limit."""
    return int(min(top_k, policy.max_results, limits.get("top_k", top_k)))


def is_caller_correctable(exc: BaseException) -> bool:
    """Whether ``call_tool`` refused ``exc`` before the tool itself ran.

    Such a refusal keeps its exception type, so the audit record and the HTTP
    status mapping are unchanged.
    """
    return getattr(exc, _CALLER_CORRECTABLE, False) is True
