"""GitHub source type: one ref of one repository."""

from __future__ import annotations

from pydantic import Field

from .common import HttpTuning
from .fields import HttpUrl, Secret, StrictModel
from .spec import AuthMethodSpec, Capabilities, Deployment, SourceTypeSpec

_API_HELP = "REST API root; change for GitHub Enterprise Server"


class GitHubTokenCredentials(StrictModel):
    """A personal access token or fine-grained token."""

    token: Secret = Field(
        description="Token with read access to the repository", examples=["github_pat_11A…"]
    )
    api_url: HttpUrl | None = Field(default=None, description=_API_HELP)
    web_url: HttpUrl | None = Field(default=None, description="Web root used for document links")
    api_version: str | None = Field(default=None, description="X-GitHub-Api-Version header")


class GitHubPublicCredentials(StrictModel):
    """Anonymous access to a public repository."""

    api_url: HttpUrl | None = Field(default=None, description=_API_HELP)
    web_url: HttpUrl | None = Field(default=None, description="Web root used for document links")
    api_version: str | None = Field(default=None, description="X-GitHub-Api-Version header")


class GitHubConfig(StrictModel):
    """What to sync from a repository."""

    repository_url: HttpUrl | None = Field(
        default=None,
        description="Repository URL; or set owner and repo",
        examples=["https://github.com/acme/handbook"],
    )
    owner: str | None = Field(default=None, description="Repository owner")
    repo: str | None = Field(default=None, description="Repository name")
    ref: str | None = Field(default=None, description="Branch, tag or commit to read")
    branch: str | None = Field(default=None, description="Branch to read")
    commit_sha: str | None = Field(default=None, description="Commit to read")
    root_path: str | None = Field(default=None, description="Folder inside the repository")
    allowed_extensions: list[str] = Field(default_factory=list, description="File types to keep")
    excluded_extensions: list[str] = Field(default_factory=list, description="File types to drop")
    include_paths: list[str] = Field(default_factory=list)
    exclude_paths: list[str] = Field(default_factory=list)
    include_globs: list[str] = Field(default_factory=list)
    exclude_globs: list[str] = Field(default_factory=list)
    max_file_size_bytes: int | None = Field(default=None, ge=0)
    fail_on_error: bool = Field(default=False, description="Stop the run on the first item error")


GITHUB = SourceTypeSpec(
    source_type="github",
    name="GitHub",
    category="code",
    provider="github",
    auth_methods=(
        AuthMethodSpec("pat", "Access token", GitHubTokenCredentials),
        AuthMethodSpec("none", "Public repository", GitHubPublicCredentials),
    ),
    config_model=GitHubConfig,
    tuning_model=HttpTuning,
    capabilities=Capabilities(incremental=True, attachments=False),
    aliases=("github_repo", "github_repository"),
    deployments=(
        Deployment(
            "github_com",
            "GitHub.com",
            "pat",
            "Sign in with a token that can read the repository; set method to none and leave "
            "out the token for a public repository",
        ),
        Deployment(
            "enterprise",
            "GitHub Enterprise Server",
            "pat",
            "Sign in with a token from your GitHub Enterprise Server",
            credentials={
                "api_url": "https://github.acme.internal/api/v3",
                "web_url": "https://github.acme.internal",
            },
            config={"repository_url": "https://github.acme.internal/acme/handbook"},
        ),
    ),
)
