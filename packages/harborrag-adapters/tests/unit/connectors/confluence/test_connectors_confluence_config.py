"""Unit tests for Confluence connector configuration validation."""

from __future__ import annotations

import pytest
from confluence_test_helpers import CLOUD_BASE, DC_BASE, cloud_config, dc_config
from harborrag_adapters.connectors.confluence import (
    ConfluenceDeploymentType,
    ConfluenceSpaceConfig,
)

pytestmark = [pytest.mark.unit, pytest.mark.graybox]


def test_config_auto_detects_cloud_and_datacenter():
    assert cloud_config().deployment_type == ConfluenceDeploymentType.CLOUD
    assert dc_config().deployment_type == ConfluenceDeploymentType.DATACENTER


def test_config_requires_cloud_email_and_rejects_bad_content_type():
    with pytest.raises(ValueError, match="email is required"):
        ConfluenceSpaceConfig(space_key="ENG", base_url=CLOUD_BASE, token="token")
    with pytest.raises(ValueError, match="content_types"):
        cloud_config(content_types=["page", "comment"])


def test_config_rejects_missing_token():
    with pytest.raises(ValueError, match="token is required"):
        ConfluenceSpaceConfig(space_key="ENG", base_url=DC_BASE, token=None, email=None)


def test_config_accepts_deployment_type_enum_directly():
    cfg = ConfluenceSpaceConfig(
        space_key="ENG",
        base_url=DC_BASE,
        token="pat",
        deployment_type=ConfluenceDeploymentType.CLOUD,
        email="me@example.com",
    )
    assert cfg.deployment_type == ConfluenceDeploymentType.CLOUD


def test_config_rejects_out_of_range_requests_per_minute():
    with pytest.raises(ValueError, match="requests_per_minute must be between"):
        dc_config(requests_per_minute=0)


def test_config_rejects_out_of_range_page_size():
    with pytest.raises(ValueError, match="page_size must be between"):
        dc_config(page_size=0)
