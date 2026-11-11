"""Table-mode views of operation bodies, for a person at a terminal.

Handles:
- Lists (a body with ``items``): one row per item with the identifiers, name, source type,
  status, stage, error category and one timestamp that any item has, then
  ``More: --cursor <next_cursor>`` when a page follows; items with none of those print as a tree
- A chat completion: the answer text, then its citations as a numbered list
- Single objects: a key/value tree, where a stored secret's status reads ``set ****1234``

Does NOT change what JSON or YAML print; it draws the same serialized body. Values are
printed as plain text, never read as Rich markup.

Start from: render, render_doctor.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.tree import Tree

_IDENTIFIER_FIELDS = (
    "project_id",
    "connection_id",
    "connection_ref",
    "ingestion_id",
    "failure_id",
    "session_id",
)
_REF_PARTS = frozenset({"project_id", "connection_id"})
_NAME_FIELDS = ("name", "project_name", "title")
_LABEL_FIELDS = ("source_type", "status", "stage", "error_category")
_TIMESTAMP_FIELDS = ("updated_at", "created_at", "finished_at")
_CHECK_GLYPHS = {
    "ok": ("✓", "green"),
    "fail": ("✗", "red"),
    "warn": ("!", "yellow"),
    "skip": ("–", "dim"),
}
_EMPTY = "-"
_MASK = "****"


def render(operation_id: str | None, data: object) -> None:
    """Print an operation's serialized body as a table or a key/value tree.

    Args:
        operation_id: Operation whose body ``data`` is; ``None`` for a CLI-only command.
        data: The body as ``dump_body`` serialized it.
    """
    console = Console(highlight=False)
    if operation_id == "create_chat_completion" and isinstance(data, Mapping):
        _print_completion(console, data)
        return
    if isinstance(data, Mapping) and isinstance(data.get("items"), list):
        rows = [item for item in data["items"] if isinstance(item, Mapping)]
        columns = _columns(rows)
        if columns or not rows:
            _print_table(console, rows, columns, data.get("next_cursor"))
            return
    tree = Tree("", hide_root=True)
    _add_children(tree, data)
    console.print(tree)


def render_doctor(data: Mapping[str, object]) -> None:
    """Print the ``doctor`` report: one row per check, the hints of failed ones, the tenants.

    Args:
        data: The report's payload, with ``checks``, ``ready`` and ``tenants``.
    """
    console = Console(highlight=False)
    table = Table(box=box.SIMPLE, expand=True, pad_edge=False)
    table.add_column("", width=2, justify="center")
    table.add_column("Check", style="bold", no_wrap=True)
    table.add_column("Detail", ratio=1, overflow="fold")
    hints: list[str] = []
    checks = data.get("checks")
    for check in checks if isinstance(checks, list) else []:
        if not isinstance(check, Mapping):
            continue
        status = str(check.get("status"))
        glyph, style = _CHECK_GLYPHS.get(status, ("?", "dim"))
        table.add_row(
            Text(glyph, style=style),
            Text(_scalar(check.get("name"))),
            Text(_scalar(check.get("detail")), style="" if status == "ok" else style),
        )
        hint = str(check.get("hint") or "")
        if status == "fail" and hint and hint not in hints:
            hints.append(hint)
    ready = bool(data.get("ready"))
    title = Text("✓ Ready" if ready else "✗ Not ready", style="bold green" if ready else "bold red")
    console.print(Panel(table, title=title, border_style="green" if ready else "red"))
    for hint in hints:
        console.print(Text(f"→ {hint}", style="yellow"))
    tenants = data.get("tenants")
    names = ", ".join(str(name) for name in tenants) if isinstance(tenants, list) else ""
    console.print(Text(f"Tenants with data: {names or _EMPTY}"))


def _print_completion(console: Console, data: Mapping[str, object]) -> None:
    """The answer, then ``[n] title (url)`` for each citation."""
    console.print(Text(_scalar(data.get("answer"))))
    citations = data.get("citations")
    if not isinstance(citations, list) or not citations:
        return
    console.print()
    for citation in citations:
        if isinstance(citation, Mapping):
            link = f" ({citation['url']})" if citation.get("url") else ""
            console.print(Text(f"[{citation.get('n')}] {_scalar(citation.get('title'))}{link}"))


def _print_table(
    console: Console,
    rows: Sequence[Mapping[str, object]],
    columns: Sequence[str],
    next_cursor: object,
) -> None:
    """One table row per item, or ``No items.``, then the hint for the next page."""
    if rows:
        table = Table(box=box.SIMPLE)
        for column in columns:
            table.add_column(column, overflow="fold")
        for row in rows:
            table.add_row(*(Text(_scalar(row.get(column))) for column in columns))
        console.print(table)
    else:
        console.print("No items.")
    if next_cursor:
        console.print(Text(f"More: --cursor {next_cursor}"))


def _columns(rows: Sequence[Mapping[str, object]]) -> tuple[str, ...]:
    """Identifiers, name, labels and one timestamp, from the keys of every row in first-seen order.

    A body serialized without unset fields can leave a key out of some rows only.
    """
    keys = dict.fromkeys(key for row in rows for key in row)
    identifiers = [key for key in keys if key in _IDENTIFIER_FIELDS]
    if "connection_ref" in keys:
        identifiers = [key for key in identifiers if key not in _REF_PARTS]
    names = [key for key in _NAME_FIELDS if key in keys][:1]
    labels = [key for key in _LABEL_FIELDS if key in keys]
    timestamps = [key for key in _TIMESTAMP_FIELDS if key in keys][:1]
    return (*identifiers, *names, *labels, *timestamps)


def _add_children(node: Tree, value: object) -> None:
    """Add a mapping's keys, or a list's entries, as children of ``node``."""
    if isinstance(value, Mapping):
        for key, child in value.items():
            _add(node, str(key), child)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _add(node, f"[{index}]", child)
    else:
        node.add(Text(_scalar(value)))


def _add(node: Tree, label: str, value: object) -> None:
    """Add ``label: value`` as a leaf, or as a branch when the value nests."""
    if isinstance(value, Mapping) and _is_secret_status(value):
        node.add(Text(f"{label}: {_secret(value)}"))
    elif _nests(value):
        _add_children(node.add(Text(label)), value)
    elif isinstance(value, list) and value:
        node.add(Text(f"{label}: {', '.join(_scalar(entry) for entry in value)}"))
    else:
        node.add(Text(f"{label}: {_scalar(value)}"))


def _nests(value: object) -> bool:
    """Whether the value is a non-empty mapping or a list holding a mapping or list."""
    if isinstance(value, Mapping):
        return bool(value)
    return isinstance(value, list) and any(isinstance(entry, Mapping | list) for entry in value)


def _is_secret_status(value: Mapping[str, object]) -> bool:
    """Whether the mapping is a stored secret's status, ``{set, last4, …}``, not the secret."""
    return isinstance(value.get("set"), bool) and "last4" in value


def _secret(status: Mapping[str, object]) -> str:
    """``set ****1234``, ``set`` or ``not set``."""
    if not status.get("set"):
        return "not set"
    last4 = status.get("last4")
    return f"set {_MASK}{last4}" if last4 else "set"


def _scalar(value: object) -> str:
    """A leaf value as text; ``-`` for null or empty."""
    if value is None or value == {} or value == []:
        return _EMPTY
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)
