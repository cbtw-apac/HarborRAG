"""Unit tests for SharePoint connector configuration validation."""

from __future__ import annotations

import pytest
from sharepoint_test_helpers import SITE_URL, config

from harborrag_adapters.connectors import SharePointSiteConfig
from harborrag_adapters.connectors.sharepoint.drive_paths import parse_sharepoint_site_url

pytestmark = [pytest.mark.unit, pytest.mark.graybox]


def test_config_parses_site_url_and_normalizes_extensions():
    cfg = config(allowed_extensions={"docx", ".PDF"}, excluded_extensions={"tmp"})

    assert cfg.hostname == "contoso.sharepoint.com"
    assert cfg.site_path == "sites/Engineering"
    assert cfg.allowed_extensions == {".docx", ".pdf"}
    assert cfg.excluded_extensions == {".tmp"}
    assert parse_sharepoint_site_url(SITE_URL) == (
        "contoso.sharepoint.com",
        "sites/Engineering",
    )


def test_config_requires_site_id_or_site_url():
    with pytest.raises(ValueError, match="requires either site_id or site_url"):
        SharePointSiteConfig(access_token="token")


def test_config_requires_access_token_or_client_credentials():
    with pytest.raises(ValueError, match="access_token or client credentials"):
        SharePointSiteConfig(site_url=SITE_URL)


def test_config_rejects_out_of_range_requests_per_minute():
    with pytest.raises(ValueError, match="requests_per_minute must be between"):
        config(requests_per_minute=0)


def test_config_rejects_out_of_range_page_size():
    with pytest.raises(ValueError, match="page_size must be between"):
        config(page_size=0)
