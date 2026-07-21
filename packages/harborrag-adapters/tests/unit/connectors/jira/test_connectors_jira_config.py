"""Unit tests for Jira connector configuration validation."""

from __future__ import annotations

import pytest
from harborrag_adapters.connectors.jira import JiraDeploymentType, JiraProjectConfig
from jira_test_helpers import CLOUD_BASE, DC_BASE, cloud_config, dc_config

pytestmark = [pytest.mark.unit, pytest.mark.graybox]


def test_config_auto_detects_cloud_and_datacenter():
    assert cloud_config().deployment_type == JiraDeploymentType.CLOUD
    assert dc_config().deployment_type == JiraDeploymentType.DATACENTER


def test_config_requires_cloud_email():
    with pytest.raises(ValueError, match="email is required"):
        JiraProjectConfig(base_url=CLOUD_BASE, token="token")


def test_config_requires_token_when_env_vars_absent(monkeypatch):
    monkeypatch.delenv("JIRA_TOKEN", raising=False)
    monkeypatch.delenv("JIRA_API_TOKEN", raising=False)
    with pytest.raises(ValueError, match="token is required"):
        JiraProjectConfig(base_url=DC_BASE, deployment_type=JiraDeploymentType.DATACENTER)


def test_config_rejects_out_of_range_requests_per_minute():
    with pytest.raises(ValueError, match="requests_per_minute"):
        dc_config(requests_per_minute=0)


def test_config_rejects_out_of_range_page_size():
    with pytest.raises(ValueError, match="page_size"):
        dc_config(page_size=0)


def test_config_requested_fields_returns_explicit_fields_when_not_all():
    config = dc_config(include_all_fields=False, fields=("summary", "status"))
    assert config.requested_fields() == ("summary", "status")


def test_config_rejects_non_https_base_url():
    with pytest.raises(ValueError, match="HTTPS URL"):
        dc_config(base_url="http://jira.example.com")


def test_config_rejects_base_url_with_embedded_credentials():
    with pytest.raises(ValueError, match="HTTPS URL"):
        dc_config(base_url="https://user:pass@jira.example.com")
