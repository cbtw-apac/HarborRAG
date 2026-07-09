"""Environment-driven config builders for the live smoke test suite.

Nothing in this module makes network calls or imports connector modules at
import time — it only reads ``os.environ`` and reports which required
variables are missing so tests can skip with a clear reason instead of
failing. See ``.env.example`` at the repo root for the full variable list.
"""
from __future__ import annotations

import os
from pathlib import Path


def _env(name: str) -> str | None:
    value = os.getenv(name)
    return value.strip() if value else None


# --------------------------------------------------------------------------- #
# Confluence
# --------------------------------------------------------------------------- #
def confluence_missing_vars() -> list[str]:
    return [
        name
        for name in ("CONFLUENCE_BASE_URL", "CONFLUENCE_SPACE_KEY", "CONFLUENCE_TOKEN")
        if not _env(name)
    ]


def confluence_config():
    from harborrag_adapters.connectors.confluence.config import ConfluenceSpaceConfig

    return ConfluenceSpaceConfig(
        space_key=_env("CONFLUENCE_SPACE_KEY"),
        base_url=_env("CONFLUENCE_BASE_URL"),
        token=_env("CONFLUENCE_TOKEN"),
        email=_env("CONFLUENCE_EMAIL"),
        include_comments=False,
        include_attachments=False,
    )


# --------------------------------------------------------------------------- #
# JIRA
# --------------------------------------------------------------------------- #
def jira_missing_vars() -> list[str]:
    missing = [] if _env("JIRA_BASE_URL") else ["JIRA_BASE_URL"]
    if not (_env("JIRA_TOKEN") or _env("JIRA_API_TOKEN")):
        missing.append("JIRA_TOKEN (or JIRA_API_TOKEN)")
    return missing


def jira_config():
    from harborrag_adapters.connectors.jira.config import JiraProjectConfig

    project_key = _env("JIRA_PROJECT_KEY")
    return JiraProjectConfig(
        base_url=_env("JIRA_BASE_URL"),
        token=_env("JIRA_TOKEN") or _env("JIRA_API_TOKEN"),
        email=_env("JIRA_EMAIL"),
        project_keys=[project_key] if project_key else [],
        include_comments=False,
        include_attachments=False,
        include_changelog=False,
    )


# --------------------------------------------------------------------------- #
# GitHub
# --------------------------------------------------------------------------- #
def github_missing_vars() -> list[str]:
    missing = []
    if not _env("GITHUB_REPOSITORY_URL") and not (_env("GITHUB_OWNER") and _env("GITHUB_REPO")):
        missing.append("GITHUB_OWNER+GITHUB_REPO (or GITHUB_REPOSITORY_URL)")
    if not _env("GITHUB_TOKEN"):
        missing.append("GITHUB_TOKEN")
    return missing


def github_config():
    from harborrag_adapters.connectors.github.config import GitHubRepositoryConfig

    return GitHubRepositoryConfig(
        owner=_env("GITHUB_OWNER"),
        repo=_env("GITHUB_REPO"),
        repository_url=_env("GITHUB_REPOSITORY_URL"),
        token=_env("GITHUB_TOKEN"),
        ref=_env("GITHUB_REF"),
    )


# --------------------------------------------------------------------------- #
# SharePoint
# --------------------------------------------------------------------------- #
def sharepoint_missing_vars() -> list[str]:
    missing = []
    if not _env("SHAREPOINT_SITE_URL") and not _env("SHAREPOINT_SITE_ID"):
        missing.append("SHAREPOINT_SITE_URL (or SHAREPOINT_SITE_ID)")
    has_token = bool(_env("MICROSOFT_GRAPH_TOKEN"))
    has_client_creds = bool(
        _env("MICROSOFT_TENANT_ID")
        and _env("MICROSOFT_CLIENT_ID")
        and _env("MICROSOFT_CLIENT_SECRET")
    )
    if not has_token and not has_client_creds:
        missing.append(
            "MICROSOFT_GRAPH_TOKEN (or MICROSOFT_TENANT_ID+MICROSOFT_CLIENT_ID+"
            "MICROSOFT_CLIENT_SECRET)"
        )
    return missing


def sharepoint_config():
    from harborrag_adapters.connectors.sharepoint.config import SharePointSiteConfig

    return SharePointSiteConfig(
        site_url=_env("SHAREPOINT_SITE_URL"),
        site_id=_env("SHAREPOINT_SITE_ID"),
        drive_name=_env("SHAREPOINT_DRIVE_NAME"),
        access_token=_env("MICROSOFT_GRAPH_TOKEN"),
        tenant_id=_env("MICROSOFT_TENANT_ID"),
        client_id=_env("MICROSOFT_CLIENT_ID"),
        client_secret=_env("MICROSOFT_CLIENT_SECRET"),
    )


# --------------------------------------------------------------------------- #
# Local filesystem
# --------------------------------------------------------------------------- #
def local_missing_vars() -> list[str]:
    path = _env("LOCAL_SOURCE_PATH")
    if not path:
        return ["LOCAL_SOURCE_PATH"]
    if not Path(path).expanduser().exists():
        return [f"LOCAL_SOURCE_PATH ({path} does not exist)"]
    return []


def local_config():
    from harborrag_adapters.connectors.local.config import LocalFileConfig

    return LocalFileConfig(source_path=_env("LOCAL_SOURCE_PATH"))


# --------------------------------------------------------------------------- #
# Sample documents for parser live tests
# --------------------------------------------------------------------------- #
SAMPLE_DOC_VARS = {
    "pdf": "SAMPLE_PDF_PATH",
    "docx": "SAMPLE_DOCX_PATH",
    "pptx": "SAMPLE_PPTX_PATH",
    "xlsx": "SAMPLE_XLSX_PATH",
}


def sample_doc_path(kind: str) -> Path | None:
    value = _env(SAMPLE_DOC_VARS[kind])
    if not value:
        return None
    path = Path(value).expanduser()
    return path if path.exists() else None
