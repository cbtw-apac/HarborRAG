"""HarborRAG Explorer: an MCP App for searching the corpus through the reader tools.

MCP Apps let a tool answer with an interactive UI that the host renders in a
sandboxed frame (Claude, ChatGPT, VS Code, the MCP Inspector ``Apps`` tab).
FastMCP describes that UI with Prefab components: the entry tool returns a
``PrefabApp`` and the UI calls backend tools through ``CallTool``.

The explorer is served by its own MCP server (``harborrag-mcp-ui``), so the
reader server's catalog stays the eleven reader tools. It adds no data path of
its own: every backend delegates to the same audited handler the reader tools
are registered with, so a search from the UI is authenticated, tenant-bound,
policy-checked, schema-validated and audited like a ``vector_search`` call from
a model. A host that lets the model call a backend directly reaches that same
boundary, nothing wider.
"""

# No ``from __future__ import annotations``: FastMCP builds each tool schema from
# the handler's evaluated signature, and the handlers below annotate with names
# that only exist inside ``build_explorer``.

from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import TYPE_CHECKING, Any, Literal

from harborrag_mcp_server.server.explorer import views
from harborrag_mcp_server.server.explorer.ui import ExplorerLayout, explorer_view
from harborrag_mcp_server.server.http_auth import allowed_tenants

if TYPE_CHECKING:
    from fastmcp import FastMCPApp

    from harborrag_core.contracts.tools import ToolSpec
    from harborrag_mcp_server.server.server import McpServer

type ToolHandler = Callable[..., Awaitable[dict[str, object]]]
type SearchMode = Literal["evidence", "entities"]
type SearchLane = Literal["hybrid", "dense", "sparse"]
type BrowseKind = Literal["documents", "sources"]
type GraphAction = Literal["resolve", "expand"]
type SelectorKind = Literal["exact_title", "provider_id", "node_key"]

EXPLORER_APP_NAME = "HarborRAG Explorer"
EXPLORER_ENTRY_TOOL = "open_explorer"
EXPLORER_INSTRUCTIONS = (
    "Call open_explorer when the user wants to search, read or trace the HarborRAG "
    "corpus themselves. It shows them an interactive UI; evidence they choose comes "
    "back as a chat message citing evidence IDs."
)

# Each backend serves several modes; a mode exists only while its reader tool is
# enabled, and a backend only while it has a mode.
_BACKENDS: dict[str, dict[str, str]] = {
    "explorer_search": {"evidence": "vector_search", "entities": "find_entities"},
    "explorer_read": {"evidence": "fetch_evidence", "document": "get_document_context"},
    "explorer_browse": {"documents": "list_documents", "sources": "list_sources"},
    "explorer_graph": {"resolve": "resolve_graph_nodes", "expand": "graph_subgraph_search"},
}
_LIMIT_FIELDS = {
    "vector_search": ("top_k", 20),
    "find_entities": ("limit", 20),
    "get_document_context": ("limit", 10),
}
_EVIDENCE_WINDOW = 5
_EMPTY_READER: dict[str, Any] = {
    "kind": "",
    "title": "",
    "section": "",
    "text": "",
    "evidence_id": "",
    "document_id": "",
    "document_version_id": "",
    "connector_type": "",
    "chunks": [],
    "outline": [],
    "next_cursor": "",
    "summary": "",
    "chat_message": "",
    "context_message": "",
}


def _modes_for(enabled: set[str]) -> dict[str, dict[str, str]]:
    return {
        backend: {mode: reader for mode, reader in modes.items() if reader in enabled}
        for backend, modes in _BACKENDS.items()
    }


def _tool_names_for(modes: dict[str, dict[str, str]]) -> list[str]:
    backends = [name for name, backend_modes in modes.items() if backend_modes]
    return [*backends, EXPLORER_ENTRY_TOOL] if backends else []


def explorer_modes(registry: "McpServer") -> dict[str, dict[str, str]]:
    """The enabled modes of every backend, each mapped to the reader tool it calls."""

    return _modes_for({spec.name for spec in registry.list_tools()})


def explorer_tool_names(registry: "McpServer") -> list[str]:
    """The tool names the explorer registers for this registry, in order."""

    return _tool_names_for(explorer_modes(registry))


def build_explorer(  # noqa: C901 - one closure per backend shares the registry state
    registry: "McpServer",
    handler: Callable[[str], ToolHandler],
) -> "FastMCPApp | None":
    """Build the explorer app, or ``None`` when every tool it needs is disabled.

    ``handler`` builds the audited transport handler for a reader tool name.
    It is passed in rather than imported so this package stays independent of
    the transport factory that owns it.
    """

    from fastmcp import FastMCPApp
    from fastmcp.exceptions import ToolError
    from mcp.types import ToolAnnotations
    from prefab_ui.app import PrefabApp

    # One pass over the registry: every derived view below (modes, names,
    # limits, catalog) comes from this list rather than re-resolving the
    # configured tool table for each of them.
    specs = registry.list_tools()
    enabled = {spec.name for spec in specs}
    modes = _modes_for(enabled)
    names = _tool_names_for(modes)
    if not names:
        return None
    limits = {
        tool: _schema_maximum(specs, tool, name, default=default)
        for tool, (name, default) in _LIMIT_FIELDS.items()
    }
    app = FastMCPApp(EXPLORER_APP_NAME)

    def reader_for(backend: str, mode: str) -> str:
        reader = modes[backend].get(mode)
        if reader is None:
            raise ToolError(f"{mode} is not enabled on this server.")
        return reader

    # Named after the tools they register as: a Prefab ``CallTool`` reference
    # resolves by the function's ``__name__``, not by the registered name.
    async def explorer_search(
        query: str,
        tenant_id: str = "",
        mode: SearchMode = "evidence",
        top_k: int = 5,
        lane: SearchLane = "hybrid",
    ) -> dict[str, Any]:
        """Search the tenant corpus for the explorer UI."""

        text = query.strip()
        if not text:
            raise ToolError("Enter a question or keywords to search.")
        reader = reader_for("explorer_search", mode)
        tenant = _require_tenant(tenant_id, ToolError)
        size = max(1, min(int(top_k), limits[reader]))
        if mode == "entities":
            result = await handler(reader)(query=text, tenant_id=tenant, limit=size)
            rows = views.entity_rows(result)
            return _search_result(
                "entities", mode, text, tenant, [], rows, views.entity_chat_message(text, rows)
            )
        result = await handler(reader)(query=text, tenant_id=tenant, top_k=size, lane=lane)
        hits = views.search_hits(result)
        message = views.search_chat_message(text, hits, "search")
        return _search_result("evidence", mode, text, tenant, hits, [], message)

    async def explorer_read(
        tenant_id: str = "",
        evidence_id: str = "",
        document_id: str = "",
        cursor: str = "",
    ) -> dict[str, Any]:
        """Open one evidence chunk with its reading window, or a window of a document."""

        tenant = _require_tenant(tenant_id, ToolError)
        chunk_id, document = evidence_id.strip(), document_id.strip()
        if chunk_id:
            reader = reader_for("explorer_read", "evidence")
            request: dict[str, str] = {"chunk_id": chunk_id}
            if document:
                request["expected_document_id"] = document
            fetched = await handler(reader)(tenant_id=tenant, items=[request])
            items = fetched.get("items")
            item = items[0] if isinstance(items, list) and items else None
            if not isinstance(item, Mapping) or item.get("availability") != "available":
                raise ToolError(
                    "This evidence is unavailable: it may be unpublished, replaced, "
                    "or outside your access."
                )
            return views.evidence_reader(item, await evidence_window(tenant, item))
        if not document:
            raise ToolError("Choose a search hit, an entity or a document to read.")
        reader = reader_for("explorer_read", "document")
        arguments: dict[str, Any] = {
            "tenant_id": tenant,
            "document_id": document,
            "limit": limits[reader],
        }
        if cursor.strip():
            arguments["cursor"] = cursor.strip()
        else:
            arguments["include_outline"] = True
        # The context window carries the document title itself, so the reader
        # view needs no second, separately audited metadata call.
        return views.document_reader(await handler(reader)(**arguments))

    async def evidence_window(
        tenant: str, item: Mapping[str, object]
    ) -> Mapping[str, object] | None:
        """The chunks around a fetched evidence item; optional, so failures are dropped."""

        document = item.get("document_id")
        if "get_document_context" not in enabled or not isinstance(document, str):
            return None
        arguments: dict[str, Any] = {
            "tenant_id": tenant,
            "document_id": document,
            "anchor_chunk_id": item.get("chunk_id"),
            "limit": min(_EVIDENCE_WINDOW, limits["get_document_context"]),
        }
        version = item.get("document_version_id")
        if isinstance(version, str) and version:
            arguments["expected_document_version_id"] = version
        try:
            return await handler("get_document_context")(**arguments)
        except ToolError:
            return None

    async def explorer_browse(
        tenant_id: str = "",
        kind: BrowseKind = "documents",
        cursor: str = "",
    ) -> dict[str, Any]:
        """Page through the tenant's documents or sources."""

        reader = reader_for("explorer_browse", kind)
        arguments: dict[str, Any] = {"tenant_id": _require_tenant(tenant_id, ToolError)}
        if cursor.strip():
            arguments["cursor"] = cursor.strip()
        return views.browse_rows(kind, await handler(reader)(**arguments))

    async def explorer_graph(
        tenant_id: str = "",
        action: GraphAction = "resolve",
        value: str = "",
        selector_kind: SelectorKind = "exact_title",
    ) -> dict[str, Any]:
        """Resolve a graph node, or expand the neighborhood around one."""

        text = value.strip()
        if not text:
            raise ToolError("Enter a title, a provider ID or a node key.")
        reader = reader_for("explorer_graph", action)
        tenant = _require_tenant(tenant_id, ToolError)
        if action == "resolve":
            result = await handler(reader)(
                tenant_id=tenant, selector={"kind": selector_kind, "value": text}
            )
            return views.candidate_rows(result)
        return views.neighborhood(text, await handler(reader)(tenant_id=tenant, start_node=text))

    backends: dict[str, ToolHandler] = {}
    for function in (explorer_search, explorer_read, explorer_browse, explorer_graph):
        if function.__name__ in names:
            app.tool(timeout=120)(function)
            backends[function.__name__] = function

    search_modes = list(modes["explorer_search"])
    layout = ExplorerLayout(
        search=backends.get("explorer_search"),
        read=backends.get("explorer_read"),
        browse=backends.get("explorer_browse"),
        graph=backends.get("explorer_graph"),
        search_modes=search_modes,
        browse_kinds=list(modes["explorer_browse"]),
        graph_actions=list(modes["explorer_graph"]),
        top_k_limit=max(
            (limits[_BACKENDS["explorer_search"][mode]] for mode in search_modes), default=20
        ),
        catalog=[
            {
                "name": spec.name,
                "capability": spec.capability,
                "description": _first_sentence(spec.description),
            }
            for spec in specs
        ],
    )
    search = backends.get("explorer_search")

    @app.ui(
        name=EXPLORER_ENTRY_TOOL,
        title="HarborRAG Explorer",
        description=(
            "Open the interactive HarborRAG Explorer for the user: search evidence "
            "or entities, read evidence in its document, browse documents "
            "and sources, and trace the knowledge graph. Call it when the user wants to "
            "explore the corpus themselves; it returns a UI, not citable evidence. Pass "
            "query (and optionally mode) to open with that search already run."
        ),
        annotations=ToolAnnotations(
            read_only_hint=True,
            destructive_hint=False,
            idempotent_hint=True,
            open_world_hint=False,
        ),
        timeout=120,
    )
    async def open_explorer(
        query: str = "",
        tenant_id: str = "",
        mode: SearchMode = "evidence",
    ) -> PrefabApp:
        tenant = _bound_tenant(tenant_id)
        start_mode = mode if mode in search_modes else (search_modes[0] if search_modes else "")
        initial = _search_result(start_mode, start_mode, "", tenant, [], [], "")
        error = ""
        if query.strip() and search is not None:
            try:
                initial = await search(query, tenant_id=tenant, mode=start_mode)
            except (ToolError, PermissionError) as exc:
                error = str(exc)
        empty_page = {"rows": [], "next_cursor": "", "summary": "Not loaded yet."}
        return PrefabApp(
            title="HarborRAG Explorer",
            view=explorer_view(layout, tenant=tenant, query=query, mode=start_mode),
            state={
                "tenant_id": tenant,
                "query": query,
                "mode": start_mode,
                "top_k": 5,
                "lane": "hybrid",
                "tab": "search" if search is not None else "documents",
                "search": initial,
                "reader": dict(_EMPTY_READER),
                "documents": dict(empty_page),
                "sources": dict(empty_page),
                "graph": {"candidates": [], "summary": ""},
                "graph_kind": "exact_title",
                "graph_value": "",
                "neighborhood": {"nodes": [], "relations": [], "chart": "", "summary": ""},
                "error": error,
            },
        )

    del open_explorer
    return app


def _search_result(  # noqa: PLR0913 - the fields of one search result
    kind: str,
    mode: str,
    query: str,
    tenant: str,
    hits: list[dict[str, Any]],
    entities: list[dict[str, Any]],
    chat_message: str,
) -> dict[str, Any]:
    count = len(entities) if kind == "entities" else len(hits)
    label = views.SEARCH_LABELS.get(mode, mode)
    return {
        "kind": kind,
        "mode": mode,
        "query": query,
        "tenant_id": tenant,
        "hits": hits,
        "entities": entities,
        "count": count,
        "summary": f"{count} {label} result(s) for tenant {tenant}" if query else "",
        "chat_message": chat_message,
    }


def _bound_tenant(tenant_id: str) -> str:
    """Return the requested tenant, else the one tenant the token is bound to."""

    tenant = tenant_id.strip()
    if tenant:
        return tenant
    from fastmcp.server.dependencies import get_access_token

    token = get_access_token()
    if token is None:
        return ""
    grants = allowed_tenants(token.claims or {})
    if len(grants) == 1 and "*" not in grants:
        return next(iter(grants))
    return ""


def _require_tenant(tenant_id: str, error: type[Exception]) -> str:
    tenant = _bound_tenant(tenant_id)
    if not tenant:
        raise error("Enter a tenant ID; this token is not bound to a single tenant.")
    return tenant


def _first_sentence(description: str) -> str:
    head, separator, _ = description.partition(". ")
    return head + "." if separator else description


def _schema_maximum(specs: "Sequence[ToolSpec]", tool: str, field: str, *, default: int) -> int:
    spec = next((item for item in specs if item.name == tool), None)
    properties = spec.input_schema.get("properties") if spec is not None else None
    bound = properties.get(field, {}).get("maximum") if isinstance(properties, dict) else None
    return bound if isinstance(bound, int) and bound > 0 else default


__all__ = [
    "EXPLORER_APP_NAME",
    "EXPLORER_ENTRY_TOOL",
    "EXPLORER_INSTRUCTIONS",
    "build_explorer",
    "explorer_modes",
    "explorer_tool_names",
]
