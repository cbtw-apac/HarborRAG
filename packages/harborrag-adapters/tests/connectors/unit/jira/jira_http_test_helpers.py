"""Shared fake-session client builder for Jira HTTP client tests."""

from __future__ import annotations

from typing import Any


def jira_client(**overrides: Any):
    from harborrag_adapters.connectors.jira.config import (
        JiraDeploymentType,
        JiraProjectConfig,
    )
    from harborrag_adapters.connectors.jira.connector import _RequestsJiraClient

    values = {
        "base_url": "https://ex.atlassian.net",
        "email": "a@b.c",
        "token": "t",
        "deployment_type": JiraDeploymentType.CLOUD,
        "requests_per_minute": 6000,
        "max_retries": 1,
        "backoff_factor": 0.01,
    }
    values.update(overrides)
    return _RequestsJiraClient(JiraProjectConfig(**values))
