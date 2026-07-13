"""Shared fake-session client builder for Confluence HTTP client tests."""
from __future__ import annotations


def confluence_client():
    from harborrag_adapters.connectors.confluence.config import ConfluenceSpaceConfig
    from harborrag_adapters.connectors.confluence.connector import (
        _RequestsConfluenceClient,
    )

    cfg = ConfluenceSpaceConfig(
        space_key="ENG",
        base_url="https://ex.atlassian.net/wiki",
        token="t",
        email="a@b.c",
        requests_per_minute=6000,
    )
    return _RequestsConfluenceClient(cfg)
