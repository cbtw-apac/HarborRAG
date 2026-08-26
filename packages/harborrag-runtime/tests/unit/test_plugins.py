from __future__ import annotations

from types import SimpleNamespace

import pytest

from harborrag_runtime import plugins


class _EntryPoints:
    def __init__(self, entries) -> None:
        self.entries = entries

    def select(self, *, group):
        return tuple(entry for entry in self.entries if entry.group == group)


class _Plugin:
    capabilities = {"streaming": True}

    def __init__(self) -> None:
        self.registered = False

    def register(self) -> None:
        self.registered = True


def test_plugin_discovery_is_explicit_and_validates_capabilities(monkeypatch) -> None:
    product = _Plugin()
    entry = SimpleNamespace(
        group="harborrag.connectors",
        name="example",
        load=lambda: product,
    )
    monkeypatch.setattr(plugins, "entry_points", lambda: _EntryPoints([entry]))

    discovered = plugins.discover_runtime_plugins()

    assert discovered[0].product is product
    assert product.registered is True


def test_plugin_without_capabilities_is_rejected(monkeypatch) -> None:
    entry = SimpleNamespace(
        group="harborrag.parsers",
        name="invalid",
        load=lambda: SimpleNamespace(register=lambda: None),
    )
    monkeypatch.setattr(plugins, "entry_points", lambda: _EntryPoints([entry]))

    with pytest.raises(ValueError, match="does not declare capabilities"):
        plugins.discover_runtime_plugins()
