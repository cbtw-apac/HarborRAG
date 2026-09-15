"""Data-source presets: the connector blocks and .env variables `init` can scaffold."""

from __future__ import annotations

from dataclasses import dataclass

DEFAULT_SOURCE = "local"


@dataclass(frozen=True, slots=True)
class SourceVariable:
    name: str
    label: str
    secret: bool = False
    default: str = ""
    # Some answers shape the YAML instead of landing in .env (Jira project keys).
    yaml_only: bool = False


@dataclass(frozen=True, slots=True)
class SourcePreset:
    key: str
    connector_name: str
    label: str
    variables: tuple[SourceVariable, ...]
    # ``@{name}`` placeholders in the block are filled from yaml_only variables.
    block: str


_LOCAL_BLOCK = """\
  workspace:
    provider: local
    enabled: true
    environment:
      source_path: LOCAL_SOURCE_PATH
    settings:
      allowed_extensions: [md, markdown, txt, pdf, docx, pptx, csv, xlsx]
      excluded_extensions: []
      include_paths: []
      exclude_paths: []
      include_globs: []
      exclude_globs: []
      include_hidden: false
      follow_symlinks: false
      max_depth: null
      max_file_size_bytes: 104857600
      checksum_mode: stat
      fail_on_error: false
"""

_GITHUB_BLOCK = """\
  github:
    provider: github
    enabled: true
    environment:
      repository_url: GITHUB_REPOSITORY_URL
    settings:
      branch: @{GITHUB_BRANCH}
      root_path: ""
      allowed_extensions: [md, markdown, txt, pdf, docx, pptx, csv, xlsx]
      excluded_extensions: []
      include_paths: []
      exclude_paths: []
      include_globs: []
      exclude_globs: []
      max_file_size_bytes: 104857600
      fail_on_error: false
      requests_per_minute: 120
      request_timeout_seconds: 30
      max_retries: 3
      backoff_factor: 0.5
    secrets:
      token_env: GITHUB_TOKEN
"""

_CONFLUENCE_BLOCK = """\
  confluence:
    provider: confluence
    enabled: true
    environment:
      base_url: CONFLUENCE_BASE_URL
      space_key: CONFLUENCE_SPACE_KEY
    settings:
      deployment_type: cloud
      content_types: [page]
      include_labels: []
      exclude_labels: []
      include_comments: false
      include_attachments: false
      max_attachment_size_bytes: 26214400
      max_comments: 1000
      max_attachments: 1000
      max_child_pages: 1000
      fail_on_error: false
      requests_per_minute: 60
      page_size: 25
      request_timeout_seconds: 30
      max_retries: 3
      backoff_factor: 0.5
    secrets:
      token_env: CONFLUENCE_TOKEN
      email_env: CONFLUENCE_EMAIL
"""

_JIRA_BLOCK = """\
  jira:
    provider: jira
    enabled: true
    environment:
      base_url: JIRA_BASE_URL
    settings:
      deployment_type: cloud
      project_keys: [@{JIRA_PROJECT_KEYS}]
      issue_types: []
      statuses: []
      labels: []
      include_comments: true
      include_attachments: false
      include_changelog: false
      include_attachment_text_in_content: true
      max_attachment_size_bytes: 26214400
      max_comments: 1000
      max_attachments: 1000
      max_changelog_items: 1000
      fail_on_error: false
      requests_per_minute: 60
      page_size: 50
      request_timeout_seconds: 30
      max_retries: 3
      backoff_factor: 0.5
    secrets:
      token_env: JIRA_TOKEN
      email_env: JIRA_EMAIL
"""

SOURCES: dict[str, SourcePreset] = {
    "local": SourcePreset(
        key="local",
        connector_name="workspace",
        label="Local folder",
        # LOCAL_SOURCE_PATH is written from the folder prompt, not from this list.
        variables=(),
        block=_LOCAL_BLOCK,
    ),
    "github": SourcePreset(
        key="github",
        connector_name="github",
        label="GitHub repository",
        variables=(
            SourceVariable("GITHUB_REPOSITORY_URL", "GitHub repository URL"),
            SourceVariable("GITHUB_TOKEN", "GitHub token (blank to fill in later)", secret=True),
            SourceVariable("GITHUB_BRANCH", "Branch", default="main", yaml_only=True),
        ),
        block=_GITHUB_BLOCK,
    ),
    "confluence": SourcePreset(
        key="confluence",
        connector_name="confluence",
        label="Confluence space",
        variables=(
            SourceVariable("CONFLUENCE_BASE_URL", "Confluence base URL (…/wiki)"),
            SourceVariable("CONFLUENCE_SPACE_KEY", "Space key"),
            SourceVariable("CONFLUENCE_EMAIL", "Account email"),
            SourceVariable("CONFLUENCE_TOKEN", "API token (blank to fill in later)", secret=True),
        ),
        block=_CONFLUENCE_BLOCK,
    ),
    "jira": SourcePreset(
        key="jira",
        connector_name="jira",
        label="Jira project",
        variables=(
            SourceVariable("JIRA_BASE_URL", "Jira base URL"),
            SourceVariable(
                "JIRA_PROJECT_KEYS", "Project keys (comma-separated)", default="ENG", yaml_only=True
            ),
            SourceVariable("JIRA_EMAIL", "Account email"),
            SourceVariable("JIRA_TOKEN", "API token (blank to fill in later)", secret=True),
        ),
        block=_JIRA_BLOCK,
    ),
}


def parse_sources(value: str) -> tuple[str, ...]:
    """Parse ``"local, github"`` into validated, de-duplicated preset keys (order kept)."""

    keys: list[str] = []
    for raw in value.split(","):
        key = raw.strip().lower()
        if not key:
            continue
        if key not in SOURCES:
            raise ValueError(f"unknown data source {key!r}; choose from {', '.join(SOURCES)}")
        if key not in keys:
            keys.append(key)
    if not keys:
        raise ValueError(f"choose at least one data source from {', '.join(SOURCES)}")
    return tuple(keys)


__all__ = ["DEFAULT_SOURCE", "SOURCES", "SourcePreset", "SourceVariable", "parse_sources"]
