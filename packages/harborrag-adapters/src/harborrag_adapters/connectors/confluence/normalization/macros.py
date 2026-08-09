from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlsplit, urlunsplit

from harborrag_core.domain import DocumentBlockKind

_SENSITIVE_PARAMETER = re.compile(
    r"(?:authorization|credential|password|secret|session|signature|signed|token)",
    re.IGNORECASE,
)
_ALLOWED_PARAMETER_NAMES = frozenset(
    {
        "title",
        "name",
        "id",
        "local-id",
        "active",
        "layout",
        "icon",
        "type",
        "tab",
        "label",
        "labels",
    }
)


@dataclass(frozen=True, slots=True)
class MacroHandling:
    kind: DocumentBlockKind
    title: str | None
    parameters: Mapping[str, str]
    warning: str | None = None


class ConfluenceMacroHandler(Protocol):
    keys: frozenset[str]
    emits_container: bool
    emits_visible_content: bool
    emits_table: bool
    needs_rendered_fallback: bool

    def handle(self, macro_key: str, parameters: Mapping[str, object]) -> MacroHandling:
        """Describe one known macro without flattening its children."""


class _ContainerMacroHandler:
    def __init__(
        self,
        keys: frozenset[str],
        kind: DocumentBlockKind,
        *,
        default_title: str | None = None,
        emits_table: bool = False,
    ) -> None:
        self.keys = keys
        self.kind = kind
        self.default_title = default_title
        self.emits_container = True
        self.emits_visible_content = True
        self.emits_table = emits_table
        self.needs_rendered_fallback = False

    def handle(self, macro_key: str, parameters: Mapping[str, object]) -> MacroHandling:
        del macro_key
        safe = filter_macro_parameters(parameters)
        title = safe.get("title") or safe.get("name") or self.default_title
        return MacroHandling(self.kind, title, safe)


class GenericMacroHandler:
    keys: frozenset[str] = frozenset()
    emits_container = True
    emits_visible_content = True
    emits_table = False
    needs_rendered_fallback = True

    def handle(self, macro_key: str, parameters: Mapping[str, object]) -> MacroHandling:
        safe = filter_macro_parameters(parameters)
        title = safe.get("title") or safe.get("name")
        return MacroHandling(
            DocumentBlockKind.UNSUPPORTED,
            title,
            safe,
            warning=f"unsupported Confluence macro preserved: {macro_key}",
        )


class ConfluenceMacroHandlerRegistry:
    """Resolve explicit native and commonly deployed macro aliases."""

    def __init__(self, handlers: tuple[ConfluenceMacroHandler, ...] | None = None) -> None:
        selected = handlers or default_macro_handlers()
        self._handlers: dict[str, ConfluenceMacroHandler] = {}
        for handler in selected:
            for key in handler.keys:
                normalized = normalize_macro_key(key)
                if normalized in self._handlers:
                    raise ValueError(f"duplicate Confluence macro handler: {normalized}")
                self._handlers[normalized] = handler
        self._generic = GenericMacroHandler()

    def resolve(self, macro_key: str) -> ConfluenceMacroHandler:
        return self._handlers.get(normalize_macro_key(macro_key), self._generic)


def default_macro_handlers() -> tuple[ConfluenceMacroHandler, ...]:
    return (
        _ContainerMacroHandler(
            frozenset({"expand", "details"}),
            DocumentBlockKind.EXPAND,
            default_title="Details",
        ),
        _ContainerMacroHandler(
            frozenset({"panel", "info", "note", "tip", "warning"}),
            DocumentBlockKind.PANEL,
        ),
        _ContainerMacroHandler(
            frozenset({"tabs", "tab-set", "tabset", "navitabs", "aui-tabs"}),
            DocumentBlockKind.TAB_SET,
        ),
        _ContainerMacroHandler(
            frozenset({"tab", "navitab", "aui-tab"}),
            DocumentBlockKind.TAB,
        ),
        _ContainerMacroHandler(
            frozenset({"page-properties", "details-summary"}),
            DocumentBlockKind.MACRO,
            emits_table=True,
        ),
    )


def normalize_macro_key(value: str) -> str:
    return value.strip().lower().replace("_", "-").replace(" ", "-")


def filter_macro_parameters(parameters: Mapping[str, object]) -> Mapping[str, str]:
    """Keep useful display parameters while removing credentials and signed URLs."""

    safe: dict[str, str] = {}
    for raw_key, raw_value in parameters.items():
        key = str(raw_key).strip().lower()
        if key not in _ALLOWED_PARAMETER_NAMES or _SENSITIVE_PARAMETER.search(key):
            continue
        value = str(raw_value).strip()
        if not value:
            continue
        safe[key] = _safe_parameter_value(value)
    return safe


def _safe_parameter_value(value: str) -> str:
    if "://" not in value:
        return value[:512]
    parsed = urlsplit(value)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))[:512]
