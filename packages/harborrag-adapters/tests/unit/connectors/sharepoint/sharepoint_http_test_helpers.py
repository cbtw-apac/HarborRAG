"""Shared fake-session client builders for SharePoint HTTP client tests."""

from __future__ import annotations

from typing import Any


def sharepoint_client(**overrides: Any):
    from harborrag_adapters.connectors.sharepoint.config import SharePointSiteConfig
    from harborrag_adapters.connectors.sharepoint.connector import _RequestsGraphClient

    values = {
        "site_url": "https://ex.sharepoint.com/sites/s",
        "access_token": "tok",
        "requests_per_minute": 6000,
        "max_retries": 1,
        "backoff_factor": 0.01,
    }
    values.update(overrides)
    return _RequestsGraphClient(SharePointSiteConfig(**values))


def client_credentials_sharepoint_client(**overrides: Any):
    values = {
        "site_url": "https://ex.sharepoint.com/sites/s",
        "access_token": None,
        "tenant_id": "tid",
        "client_id": "cid",
        "client_secret": "secret",
        "requests_per_minute": 6000,
        "max_retries": 1,
        "backoff_factor": 0.01,
    }
    values.update(overrides)
    return sharepoint_client(**values)
