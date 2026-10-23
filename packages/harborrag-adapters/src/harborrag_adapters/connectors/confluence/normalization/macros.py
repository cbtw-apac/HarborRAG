"""Confluence macro handlers and their registry.

Handles:
- Macro classification into block kinds
- Parameter filtering that drops credentials and signed URLs
- Alias resolution with a generic fallback

Does NOT flatten macro children; the block handlers populate them.
"""

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
    """Block kind, title, safe parameters and optional warning for one macro."""

    kind: DocumentBlockKind
    title: str | None
    parameters: Mapping[str, str]
    warning: str | None = None


class ConfluenceMacroHandler(Protocol):
    """Contract for a macro handler.

    ``keys`` lists the macro names it owns; the capability flags are copied onto the block.
    """

    keys: frozenset[str]
    emits_container: bool
    emits_visible_content: bool
    emits_table: bool
    needs_rendered_fallback: bool

    def handle(self, macro_key: str, parameters: Mapping[str, object]) -> MacroHandling:
        """Describe one macro."""


class _ContainerMacroHandler:
    """Handler for macros whose body holds nested content, such as panels and tabs."""

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
        """Return the configured kind titled by ``title``, ``name`` or the default title."""

        del macro_key
        safe = filter_macro_parameters(parameters)
        title = safe.get("title") or safe.get("name") or self.default_title
        return MacroHandling(self.kind, title, safe)


class _IncludeMacroHandler:
    """Handler for ``include`` and ``excerpt-include``.

    The macro becomes a link reference; the included text is not inlined.
    """

    keys = frozenset({"include", "excerpt-include"})
    emits_container = False
    emits_visible_content = False
    emits_table = False
    needs_rendered_fallback = True

    def handle(self, macro_key: str, parameters: Mapping[str, object]) -> MacroHandling:
        """Return a link reference titled by the ``title`` or ``name`` parameter."""

        del macro_key
        safe = filter_macro_parameters(parameters)
        return MacroHandling(
            DocumentBlockKind.LINK_REFERENCE,
            safe.get("title") or safe.get("name"),
            safe,
        )


class _CodeMacroHandler:
    """Handler for ``code`` and ``noformat``, whose body is a code block."""

    keys = frozenset({"code", "noformat"})
    emits_container = False
    emits_visible_content = True
    emits_table = False
    needs_rendered_fallback = False

    def handle(self, macro_key: str, parameters: Mapping[str, object]) -> MacroHandling:
        """Return a code block titled by the ``title`` parameter."""

        del macro_key
        safe = filter_macro_parameters(parameters)
        return MacroHandling(DocumentBlockKind.CODE_BLOCK, safe.get("title"), safe)


class GenericMacroHandler:
    """Fallback handler that keeps an unknown macro as an ``unsupported`` block."""

    keys: frozenset[str] = frozenset()
    emits_container = True
    emits_visible_content = True
    emits_table = False
    needs_rendered_fallback = True

    def handle(self, macro_key: str, parameters: Mapping[str, object]) -> MacroHandling:
        """Return an unsupported block with a warning naming ``macro_key``."""

        safe = filter_macro_parameters(parameters)
        title = safe.get("title") or safe.get("name")
        return MacroHandling(
            DocumentBlockKind.UNSUPPORTED,
            title,
            safe,
            warning=f"unsupported Confluence macro preserved: {macro_key}",
        )


class ConfluenceMacroHandlerRegistry:
    """Registry resolving macro names, including common aliases, to handlers."""

    def __init__(self, handlers: tuple[ConfluenceMacroHandler, ...] | None = None) -> None:
        """Index handlers by normalized key.

        Args:
            handlers: Handlers to register; defaults to ``default_macro_handlers``.

        Raises:
            ValueError: If two handlers claim the same normalized key.
        """

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
        """Return the handler for a macro name, else the generic fallback."""

        return self._handlers.get(normalize_macro_key(macro_key), self._generic)


def default_macro_handlers() -> tuple[ConfluenceMacroHandler, ...]:
    """Return the built-in handlers for include, code, expand, panel, tabs and properties."""

    return (
        _IncludeMacroHandler(),
        _CodeMacroHandler(),
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
            frozenset(
                {
                    "tabs",
                    "tab-set",
                    "tabset",
                    "navitabs",
                    "aui-tabs",
                    "localtabgroup",
                }
            ),
            DocumentBlockKind.TAB_SET,
        ),
        _ContainerMacroHandler(
            frozenset({"tab", "navitab", "aui-tab", "localtab"}),
            DocumentBlockKind.TAB,
        ),
        _ContainerMacroHandler(
            frozenset({"page-properties", "details-summary"}),
            DocumentBlockKind.MACRO,
            emits_table=True,
        ),
    )


def normalize_macro_key(value: str) -> str:
    """Return a macro name lower-cased with ``_`` and spaces replaced by ``-``."""

    return value.strip().lower().replace("_", "-").replace(" ", "-")


def filter_macro_parameters(parameters: Mapping[str, object]) -> Mapping[str, str]:
    """Keep allow-listed display parameters, dropping credentials and signed URLs.

    Args:
        parameters: Raw macro parameters.

    Returns:
        Stripped, non-empty values keyed by lower-cased name.
    """

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
    """Truncate a value to 512 characters, removing the query and fragment of URLs."""

    if "://" not in value:
        return value[:512]
    parsed = urlsplit(value)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))[:512]
