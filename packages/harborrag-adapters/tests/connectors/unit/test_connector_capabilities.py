"""Declared connector capabilities match what each built-in connector implements."""

from __future__ import annotations

import pytest

from harborrag_adapters.connectors import connector_registry
from harborrag_adapters.connectors.base import BaseConnector

pytestmark = [pytest.mark.unit]


@pytest.mark.parametrize("provider", connector_registry.canonical_names())
def test_a_connector_declaring_native_pagination_implements_it(provider: str) -> None:
    """Runtime pages a ``pagination`` connector one cursor at a time. Without its own
    ``discover_page`` every page falls back to replaying discovery from the start."""
    connector = connector_registry.get_class(provider)

    if connector.capabilities.pagination:
        assert connector.discover_page is not BaseConnector.discover_page
