"""Prefab layout for the HarborRAG Explorer.

Prefab types text content as ``str`` and hides snake_case field names behind
pydantic aliases, so reactive values are passed as ``str(STATE...)`` (the same
``{{ ... }}`` template) and a few fields by their camelCase alias.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from prefab_ui.actions import Action

type ToolHandler = Callable[..., Awaitable[dict[str, Any]]]

SEARCH_MODE_LABELS = {
    "evidence": "Evidence search",
    "entities": "Entities",
}
BROWSE_LABELS = {"documents": "Documents", "sources": "Sources"}
SELECTOR_LABELS = {
    "exact_title": "Exact title",
    "provider_id": "Provider ID",
    "node_key": "Node key",
}


@dataclass(frozen=True, slots=True)
class ExplorerLayout:
    """What the rendered view offers: enabled backends, their modes, and the catalog."""

    search: ToolHandler | None
    read: ToolHandler | None
    browse: ToolHandler | None
    graph: ToolHandler | None
    search_modes: Sequence[str]
    browse_kinds: Sequence[str]
    graph_actions: Sequence[str]
    top_k_limit: int
    catalog: list[Any] = field(default_factory=list)


def explorer_view(layout: ExplorerLayout, *, tenant: str, query: str, mode: str) -> Any:
    from prefab_ui.components import (
        Alert,
        AlertDescription,
        AlertTitle,
        Badge,
        Column,
        Grid,
        Heading,
        If,
        Input,
        Muted,
        Row,
        Tabs,
    )
    from prefab_ui.rx import STATE

    with Column(gap=5, css_class="p-6") as view:
        with Row(gap=3, align="center", justify="between"):
            with Column(gap=1):
                Heading("HarborRAG Explorer", level=2)
                Muted("Search, read and trace evidence through the audited MCP tools.")
            Badge(label=str(STATE.tenant_id.default("no tenant")), variant="secondary")
        with Grid(columns=[1], gap=2):
            Input(
                name="tenant_id",
                value=tenant,
                placeholder="Tenant ID (leave empty when your token binds one tenant)",
            )
        with If(STATE.error), Alert(variant="destructive", icon="circle-alert"):
            AlertTitle("Request failed")
            AlertDescription(str(STATE.error))
        first = "search" if layout.search is not None else "documents"
        with Tabs(name="tab", value=first):
            if layout.search is not None:
                _search_tab(layout, query=query, mode=mode)
            if layout.read is not None:
                _reader_tab(layout)
            if layout.browse is not None:
                for kind in layout.browse_kinds:
                    _browse_tab(layout, kind)
            if layout.graph is not None:
                _graph_tab(layout)
            _tools_tab(layout.catalog)
    return view


def call(
    tool: ToolHandler,
    arguments: dict[str, Any],
    *,
    key: str,
    failure: str,
    then: Sequence[Action] = (),
) -> Action:
    """Call a backend tool, store its result under ``key``, and surface failures."""

    from prefab_ui.actions import SetState, ShowToast
    from prefab_ui.actions.mcp import CallTool
    from prefab_ui.rx import ERROR, RESULT

    return CallTool(
        tool,
        arguments=arguments,
        on_success=[SetState(key, RESULT), SetState("error", ""), *then],
        on_error=[SetState("error", ERROR), ShowToast(failure, variant="error")],
    )


def _open_in_reader(layout: ExplorerLayout, arguments: dict[str, Any]) -> Action | None:
    from prefab_ui.actions import SetState
    from prefab_ui.rx import STATE

    if layout.read is None:
        return None
    return call(
        layout.read,
        {"tenant_id": STATE.tenant_id, **arguments},
        key="reader",
        failure="Could not open it",
        then=[SetState("tab", "reader")],
    )


def _search_tab(layout: ExplorerLayout, *, query: str, mode: str) -> None:
    from prefab_ui.actions.mcp import SendMessage
    from prefab_ui.components import (
        Button,
        Column,
        DataTable,
        DataTableColumn,
        Else,
        Form,
        Grid,
        If,
        Input,
        Muted,
        Row,
        Select,
        SelectOption,
        Tab,
    )
    from prefab_ui.rx import EVENT, STATE

    if layout.search is None:
        return
    arguments = {
        "query": STATE.query,
        "tenant_id": STATE.tenant_id,
        "mode": STATE.mode,
        "top_k": STATE.top_k,
        "lane": STATE.lane,
    }
    with Tab(title="Search", value="search"), Column(gap=4):
        with Form(on_submit=call(layout.search, arguments, key="search", failure="Search failed")):
            Input(
                name="query",
                value=query,
                placeholder="Ask a question or enter keywords",
                required=True,
            )
            with Grid(columns=[2, 1, 1, "auto"], gap=3, align="end"):
                with Select(name="mode", value=mode):
                    for value in layout.search_modes:
                        SelectOption(value=value, label=SEARCH_MODE_LABELS[value])
                Input(
                    name="top_k",
                    value="5",
                    input_type="number",
                    min=1,
                    max=layout.top_k_limit,
                    step=1,
                )
                with Select(name="lane", value="hybrid"):
                    SelectOption(value="hybrid", label="Hybrid")
                    SelectOption(value="dense", label="Dense")
                    SelectOption(value="sparse", label="Sparse")
                Button("Search", icon="search", button_type="submit")
        with Row(gap=3, align="center", justify="between"):
            Muted(str(STATE.search.summary.default("Enter a question to search.")))
            with If(STATE.search.chat_message):
                Button(
                    "Ask the assistant about these results",
                    icon="message-square",
                    variant="outline",
                    on_click=SendMessage(str(STATE.search.chat_message)),
                )
        with If(STATE.search.kind == "entities"):
            DataTable(
                columns=[
                    DataTableColumn(key="rank", header="#", width="3rem"),
                    DataTableColumn(key="description", header="Entity", minWidth="18rem"),
                    DataTableColumn(key="attributes", header="Facets"),
                    DataTableColumn(key="coverage", header="Coverage"),
                    DataTableColumn(key="evidence_count", header="Evidence", align="right"),
                    DataTableColumn(key="score", header="Score", sortable=True, align="right"),
                ],
                rows=STATE.search.entities,
                search=True,
                paginated=True,
                onRowClick=_open_in_reader(layout, {"evidence_id": EVENT.first_evidence_id}),
            )
        with Else():
            DataTable(
                columns=[
                    DataTableColumn(key="rank", header="#", width="3rem"),
                    DataTableColumn(key="title", header="Document", sortable=True),
                    DataTableColumn(key="section", header="Section"),
                    DataTableColumn(key="snippet", header="Excerpt", minWidth="20rem"),
                    DataTableColumn(
                        key="relevance", header="Relevance", sortable=True, align="right"
                    ),
                ],
                rows=STATE.search.hits,
                search=True,
                paginated=True,
                onRowClick=_open_in_reader(
                    layout,
                    {"evidence_id": EVENT.evidence_id, "document_id": EVENT.document_id},
                ),
            )
        if layout.read is not None:
            Muted("Select a row to open its evidence and surrounding text in the Reader.")


def _reader_tab(layout: ExplorerLayout) -> None:
    from prefab_ui.actions import ShowToast
    from prefab_ui.actions.mcp import SendMessage, UpdateContext
    from prefab_ui.components import (
        Badge,
        Button,
        Card,
        CardContent,
        Column,
        Else,
        ForEach,
        Heading,
        If,
        Markdown,
        Muted,
        Row,
        Separator,
        Tab,
    )
    from prefab_ui.rx import STATE

    if layout.read is None:
        return
    reader = STATE.reader
    with Tab(title="Reader", value="reader"), Column(gap=4):
        with If(reader.title):
            with Row(gap=2, align="center"):
                Heading(str(reader.title), level=3)
                Badge(label=str(reader.kind), variant="secondary")
                with If(reader.connector_type):
                    Badge(label=str(reader.connector_type), variant="outline")
            Muted(str(reader.section))
            with Row(gap=2):
                Button(
                    "Send to chat",
                    icon="message-square",
                    on_click=SendMessage(str(reader.chat_message)),
                )
                Button(
                    "Add to conversation context",
                    icon="bookmark-plus",
                    variant="outline",
                    on_click=[
                        UpdateContext(content=str(reader.context_message)),
                        ShowToast("Added to the conversation context", variant="success"),
                    ],
                )
                with If(reader.kind == "evidence"):
                    Button(
                        "Read whole document",
                        icon="book-open",
                        variant="outline",
                        on_click=_open_in_reader(layout, {"document_id": reader.document_id}),
                    )
            with If(reader.text), Card(), CardContent():
                Markdown(str(reader.text))
            with If(reader.outline.length() > 0):
                Muted(str(reader.outline.join(" | ")))
            Separator()
            Heading("Reading window", level=4)
            with ForEach("reader.chunks") as chunk, Card(), CardContent(), Column(gap=2):
                with Row(gap=2, align="center"):
                    Badge(label=f"#{chunk.ordinal}", variant="outline")
                    with If(chunk.anchor):
                        Badge(label="Matched evidence")
                    Muted(str(chunk.section))
                Markdown(str(chunk.text))
            with If(reader.next_cursor):
                Button(
                    "Continue reading",
                    icon="chevrons-down",
                    variant="outline",
                    on_click=_open_in_reader(
                        layout,
                        {"document_id": reader.document_id, "cursor": reader.next_cursor},
                    ),
                )
        with Else():
            Muted("Open a search hit, an entity, or a document to read it here.")


def _browse_tab(layout: ExplorerLayout, kind: str) -> None:
    from prefab_ui.components import Button, Column, DataTable, DataTableColumn, If, Muted, Row, Tab
    from prefab_ui.rx import EVENT, STATE

    if layout.browse is None:
        return
    page = getattr(STATE, kind)
    load = call(
        layout.browse,
        {"tenant_id": STATE.tenant_id, "kind": kind},
        key=kind,
        failure=f"Could not load {kind}",
    )
    columns = (
        [
            DataTableColumn(key="title", header="Document", sortable=True),
            DataTableColumn(key="connector_type", header="Connector", sortable=True),
            DataTableColumn(key="chunk_count", header="Chunks", sortable=True, align="right"),
            DataTableColumn(key="document_id", header="Document ID"),
        ]
        if kind == "documents"
        else [
            DataTableColumn(key="name", header="Source", sortable=True),
            DataTableColumn(key="connector_type", header="Connector"),
            DataTableColumn(key="ingestion_state", header="State"),
            DataTableColumn(
                key="active_document_count", header="Documents", sortable=True, align="right"
            ),
            DataTableColumn(
                key="last_successful_ingestion_at", header="Last ingestion", sortable=True
            ),
        ]
    )
    with Tab(title=BROWSE_LABELS[kind], value=kind), Column(gap=4):
        with Row(gap=3, align="center"):
            Button(f"Load {kind}", icon="refresh-cw", on_click=load)
            Muted(str(page.summary.default("Not loaded yet.")))
            with If(page.next_cursor):
                Button(
                    "Next page",
                    icon="chevron-right",
                    variant="outline",
                    on_click=call(
                        layout.browse,
                        {"tenant_id": STATE.tenant_id, "kind": kind, "cursor": page.next_cursor},
                        key=kind,
                        failure=f"Could not load {kind}",
                    ),
                )
        DataTable(
            columns=columns,
            rows=page.rows,
            search=True,
            onRowClick=(
                _open_in_reader(layout, {"document_id": EVENT.document_id})
                if kind == "documents"
                else None
            ),
        )
        if kind == "documents" and layout.read is not None:
            Muted("Select a document to read it in the Reader.")


def _graph_tab(layout: ExplorerLayout) -> None:
    from prefab_ui.components import (
        Button,
        Column,
        DataTable,
        DataTableColumn,
        Form,
        Grid,
        Heading,
        If,
        Input,
        Mermaid,
        Muted,
        Select,
        SelectOption,
        Tab,
    )
    from prefab_ui.rx import EVENT, STATE

    if layout.graph is None:
        return
    expand = (
        call(
            layout.graph,
            {"tenant_id": STATE.tenant_id, "action": "expand", "value": EVENT.node_key},
            key="neighborhood",
            failure="Could not expand the node",
        )
        if "expand" in layout.graph_actions
        else None
    )
    with Tab(title="Graph", value="graph"), Column(gap=4):
        if "resolve" in layout.graph_actions:
            resolve = call(
                layout.graph,
                {
                    "tenant_id": STATE.tenant_id,
                    "action": "resolve",
                    "value": STATE.graph_value,
                    "selector_kind": STATE.graph_kind,
                },
                key="graph",
                failure="Could not resolve the node",
            )
            with Form(on_submit=resolve), Grid(columns=[1, 2, "auto"], gap=3, align="end"):
                with Select(name="graph_kind", value="exact_title"):
                    for value, label in SELECTOR_LABELS.items():
                        SelectOption(value=value, label=label)
                Input(
                    name="graph_value",
                    placeholder="A document title, provider ID (e.g. PAY-42) or node key",
                    required=True,
                )
                Button("Find node", icon="search", button_type="submit")
            Muted(str(STATE.graph.summary.default("Resolve a node, then select it to expand.")))
            DataTable(
                columns=[
                    DataTableColumn(key="title", header="Title", sortable=True),
                    DataTableColumn(key="entity_type", header="Type"),
                    DataTableColumn(key="node_kind", header="Kind"),
                    DataTableColumn(key="content", header="Content"),
                    DataTableColumn(key="node_key", header="Node key"),
                ],
                rows=STATE.graph.candidates,
                onRowClick=expand,
            )
        with If(STATE.neighborhood.chart):
            Heading(str(STATE.neighborhood.summary), level=4)
            Mermaid(str(STATE.neighborhood.chart))
            DataTable(
                columns=[
                    DataTableColumn(key="source", header="From"),
                    DataTableColumn(key="relation", header="Relation", sortable=True),
                    DataTableColumn(key="target", header="To"),
                    DataTableColumn(key="origin", header="Origin"),
                ],
                rows=STATE.neighborhood.relations,
                search=True,
            )
            DataTable(
                columns=[
                    DataTableColumn(key="title", header="Node", sortable=True),
                    DataTableColumn(key="entity_type", header="Type", sortable=True),
                    DataTableColumn(key="node_kind", header="Kind"),
                    DataTableColumn(key="node_key", header="Node key"),
                ],
                rows=STATE.neighborhood.nodes,
                search=True,
                onRowClick=expand,
            )
            if expand is not None:
                Muted("Select a node to expand the graph around it.")


def _tools_tab(catalog: list[Any]) -> None:
    from prefab_ui.components import Column, DataTable, DataTableColumn, Muted, Tab

    with Tab(title="Tools", value="tools"), Column(gap=4):
        Muted(
            "Tools this server advertises to models. Every call is policy-checked "
            "and written to the MCP audit trail."
        )
        DataTable(
            columns=[
                DataTableColumn(key="name", header="Tool", sortable=True),
                DataTableColumn(key="capability", header="Capability", sortable=True),
                DataTableColumn(key="description", header="Purpose"),
            ],
            rows=catalog,
            search=True,
        )


__all__ = ["ExplorerLayout", "SEARCH_MODE_LABELS", "call", "explorer_view"]
