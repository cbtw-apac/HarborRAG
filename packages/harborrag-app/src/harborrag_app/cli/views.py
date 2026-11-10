"""Table-mode views of operation bodies, for a person at a terminal.

Handles:
- Lists (a body with ``items``): one row per item with its identifiers, name, source type
  or status and one timestamp, then ``More: --cursor <next_cursor>`` when a page follows
- Single objects: a key/value tree, where a stored secret's status reads ``set ****1234``

Does NOT change what JSON or YAML print; it draws the same serialized body. Values are
printed as plain text, never read as Rich markup.

Start from: render.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from rich import box
from rich.console import Console
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
_LABEL_FIELDS = ("source_type", "status")
_TIMESTAMP_FIELDS = ("updated_at", "created_at", "finished_at")
_EMPTY = "-"
_MASK = "****"


def render(operation_id: str, data: object) -> None:
    """Print an operation's serialized body as a table or a key/value tree.

    Args:
        operation_id: Operation whose body ``data`` is.
        data: The body as ``dump_body`` serialized it.
    """
    console = Console(highlight=False)
    if isinstance(data, Mapping) and isinstance(data.get("items"), list):
        _print_list(console, data["items"], data.get("next_cursor"))
        return
    tree = Tree("", hide_root=True)
    _add_children(tree, data)
    console.print(tree)


def _print_list(console: Console, items: Sequence[object], next_cursor: object) -> None:
    """One table row per item, then the hint for the next page."""
    rows = [item for item in items if isinstance(item, Mapping)]
    columns = _columns(rows[0]) if rows else ()
    if columns:
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


def _columns(item: Mapping[str, object]) -> tuple[str, ...]:
    """Identifiers, name, source type or status, and one timestamp, as the item has them."""
    identifiers = [key for key in item if key in _IDENTIFIER_FIELDS]
    if "connection_ref" in item:
        identifiers = [key for key in identifiers if key not in _REF_PARTS]
    names = [key for key in _NAME_FIELDS if key in item][:1]
    labels = [key for key in _LABEL_FIELDS if key in item]
    timestamps = [key for key in _TIMESTAMP_FIELDS if key in item][:1]
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
