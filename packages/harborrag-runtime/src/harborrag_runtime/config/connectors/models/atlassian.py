"""Jira and Confluence source types.

Both authenticate the same way: a Cloud API token with the account email, or a
Data Center personal access token. ``api_token`` feeds the dataclass ``token``.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from .common import PagedHttpTuning
from .fields import Email, HttpUrl, Secret, StrictModel, widget
from .spec import AuthMethodSpec, Capabilities, SourceTypeSpec

DeploymentType = Literal["cloud", "datacenter"]

_BASE_URL_HELP = "Root URL of the site, without a trailing path"
_TOKEN_HELP = "Create one at id.atlassian.com › Security › API tokens"
_ALLOWED_HELP = "Attachment file types to keep, such as pdf; empty keeps every type"
_EXCLUDED_HELP = "Attachment file types to drop; wins over allowed_extensions"


class AtlassianCloudCredentials(StrictModel):
    """API token authentication for Atlassian Cloud."""

    base_url: HttpUrl = Field(description=_BASE_URL_HELP, examples=["https://acme.atlassian.net"])
    email: Email = Field(description="Account the token belongs to", examples=["bot@acme.com"])
    api_token: Secret = Field(description=_TOKEN_HELP, examples=["ATATT3x…"])
    deployment_type: DeploymentType | None = Field(
        default=None, description="Inferred from the host name when unset"
    )


class AtlassianDataCenterCredentials(StrictModel):
    """Personal access token authentication for Data Center."""

    base_url: HttpUrl = Field(description=_BASE_URL_HELP, examples=["https://jira.acme.internal"])
    api_token: Secret = Field(description="Personal access token", examples=["NjM0…"])
    deployment_type: DeploymentType | None = Field(
        default=None, description="Inferred from the host name when unset"
    )


class JiraConfig(StrictModel):
    """What to sync from Jira."""

    jql: str | None = Field(
        default=None,
        description="Extra JQL that narrows the issues to sync",
        examples=["project = CPM"],
        json_schema_extra=widget("textarea"),
    )
    project_keys: list[str] = Field(default_factory=list, description="Project keys to sync")
    issue_types: list[str] = Field(default_factory=list, description="Issue types to keep")
    statuses: list[str] = Field(default_factory=list, description="Statuses to keep")
    labels: list[str] = Field(default_factory=list, description="Labels to keep")
    include_comments: bool = Field(default=True, description="Index issue comments")
    include_attachments: bool = Field(default=False, description="Download and parse attachments")
    include_changelog: bool = Field(default=False, description="Index the issue changelog")
    include_attachment_text_in_content: bool = Field(
        default=True, description="Inline parsed attachment text in the issue document"
    )
    include_all_fields: bool = Field(default=True, description="Fetch every issue field")
    fields: list[str] | None = Field(default=None, description="Issue fields to fetch")
    max_attachment_size_bytes: int | None = Field(default=None, ge=0)
    allowed_extensions: list[str] = Field(default_factory=list, description=_ALLOWED_HELP)
    excluded_extensions: list[str] = Field(default_factory=list, description=_EXCLUDED_HELP)
    max_comments: int | None = Field(default=None, ge=0)
    max_attachments: int | None = Field(default=None, ge=0)
    max_changelog_items: int | None = Field(default=None, ge=0)
    fail_on_error: bool = Field(default=False, description="Stop the run on the first item error")


class ConfluenceConfig(StrictModel):
    """What to sync from Confluence."""

    space_key: str = Field(min_length=1, description="Space to sync", examples=["ENG"])
    content_types: list[str] = Field(default_factory=lambda: ["page"], description="page, blogpost")
    include_labels: list[str] = Field(default_factory=list, description="Keep pages with a label")
    exclude_labels: list[str] = Field(default_factory=list, description="Drop pages with a label")
    include_comments: bool = Field(default=False, description="Index page comments")
    include_attachments: bool = Field(default=False, description="Download and parse attachments")
    max_attachment_size_bytes: int | None = Field(default=None, ge=0)
    allowed_extensions: list[str] = Field(default_factory=list, description=_ALLOWED_HELP)
    excluded_extensions: list[str] = Field(default_factory=list, description=_EXCLUDED_HELP)
    max_comments: int | None = Field(default=None, ge=0)
    max_attachments: int | None = Field(default=None, ge=0)
    fail_on_error: bool = Field(default=False, description="Stop the run on the first item error")


def _auth_methods() -> tuple[AuthMethodSpec, ...]:
    """Return the auth methods Jira and Confluence share."""
    return (
        AuthMethodSpec("api_token", "API token", AtlassianCloudCredentials),
        AuthMethodSpec("pat", "Personal access token", AtlassianDataCenterCredentials),
    )


JIRA = SourceTypeSpec(
    source_type="jira",
    name="Jira",
    category="issue_tracker",
    provider="jira",
    auth_methods=_auth_methods(),
    config_model=JiraConfig,
    tuning_model=PagedHttpTuning,
    capabilities=Capabilities(incremental=True, attachments=True),
    credential_renames={"api_token": "token"},
    query_fields=frozenset({"jql"}),
    aliases=("jira_cloud", "jira_datacenter"),
)

CONFLUENCE = SourceTypeSpec(
    source_type="confluence",
    name="Confluence",
    category="wiki",
    provider="confluence",
    auth_methods=_auth_methods(),
    config_model=ConfluenceConfig,
    tuning_model=PagedHttpTuning,
    capabilities=Capabilities(incremental=True, attachments=True),
    credential_renames={"api_token": "token"},
    aliases=("confluence_cloud", "confluence_datacenter"),
)
