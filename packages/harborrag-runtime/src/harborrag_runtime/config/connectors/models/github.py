"""GitHub source type: one ref of one repository."""

from __future__ import annotations

from typing import Self

from pydantic import ConfigDict, Field, ValidationInfo, field_validator, model_validator

from .common import HttpTuning
from .fields import HttpsUrl, HttpUrl, Secret, StrictModel
from .spec import AuthMethodSpec, Capabilities, Setup, SourceTypeSpec, constant_environment

_API_HELP = "REST API root; left out, https://api.github.com. Change for GitHub Enterprise Server"
_WEB_HELP = "Web root used for document links; left out, https://github.com"
_VERSION_HELP = "X-GitHub-Api-Version header; left out, GitHub's default"


class GitHubTokenCredentials(StrictModel):
    """A personal access token or fine-grained token."""

    token: Secret = Field(
        description="Token with read access to the repository", examples=["github_pat_11A…"]
    )
    api_url: HttpsUrl | None = Field(default=None, description=_API_HELP)
    web_url: HttpsUrl | None = Field(default=None, description=_WEB_HELP)
    api_version: str | None = Field(default=None, description=_VERSION_HELP)


class GitHubPublicCredentials(StrictModel):
    """Anonymous access to a public repository."""

    api_url: HttpsUrl | None = Field(default=None, description=_API_HELP)
    web_url: HttpsUrl | None = Field(default=None, description=_WEB_HELP)
    api_version: str | None = Field(default=None, description=_VERSION_HELP)


class GitHubConfig(StrictModel):
    """What to sync from a repository: ``repository_url`` or ``owner`` and ``repo``, at one ref."""

    model_config = ConfigDict(
        json_schema_extra={
            "anyOf": [{"required": ["repository_url"]}, {"required": ["owner", "repo"]}],
            "allOf": [
                {"not": {"required": ["ref", "branch"]}},
                {"not": {"required": ["commit_sha", "ref"]}},
                {"not": {"required": ["commit_sha", "branch"]}},
            ],
        }
    )

    repository_url: HttpUrl | None = Field(
        default=None,
        description="Repository URL; or set owner and repo",
        examples=["https://github.com/acme/handbook"],
    )
    owner: str | None = Field(default=None, description="Repository owner")
    repo: str | None = Field(default=None, description="Repository name")
    ref: str | None = Field(
        default=None, description="Branch, tag or commit to read; left out, the default branch"
    )
    branch: str | None = Field(default=None, description="Branch to read; not with ref")
    commit_sha: str | None = Field(
        default=None, description="Commit to read; not with ref or branch"
    )
    root_path: str | None = Field(
        default=None, description="Folder inside the repository; left out, the whole repository"
    )
    allowed_extensions: list[str] = Field(
        default_factory=list,
        description=(
            "File types to keep, such as pdf; left out, common document and image types; "
            "[] keeps every type"
        ),
    )
    excluded_extensions: list[str] = Field(default_factory=list, description="File types to drop")
    include_paths: list[str] = Field(
        default_factory=list, description="Paths to read; empty reads every path"
    )
    exclude_paths: list[str] = Field(default_factory=list, description="Paths to skip")
    include_globs: list[str] = Field(
        default_factory=list, description="Glob patterns to read, such as docs/**/*.md"
    )
    exclude_globs: list[str] = Field(default_factory=list, description="Glob patterns to skip")
    max_file_size_bytes: int | None = Field(
        default=None, ge=0, description="Largest file to read, in bytes; left out, 20 MiB"
    )
    fail_on_error: bool = Field(default=False, description="Stop the run on the first item error")

    @field_validator("branch")
    @classmethod
    def _branch_without_ref(cls, value: str | None, info: ValidationInfo) -> str | None:
        """Refuse ``branch`` beside ``ref``, reported on ``branch``."""
        if value and info.data.get("ref"):
            raise ValueError("set ref or branch, not both")
        return value

    @field_validator("commit_sha")
    @classmethod
    def _commit_alone(cls, value: str | None, info: ValidationInfo) -> str | None:
        """Refuse ``commit_sha`` beside ``ref`` or ``branch``, reported on ``commit_sha``."""
        if value and (info.data.get("ref") or info.data.get("branch")):
            raise ValueError("set commit_sha or ref/branch, not both")
        return value

    @model_validator(mode="after")
    def _one_repository(self) -> Self:
        """Require ``repository_url``, or ``owner`` and ``repo``."""
        if not self.repository_url and not (self.owner and self.repo):
            raise ValueError("set repository_url, or owner and repo")
        return self


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
    endpoints=frozenset({"api_url", "web_url"}),
    aliases=("github_repo", "github_repository"),
    environment_defaults=constant_environment({"token": ("GITHUB_TOKEN",)}),
    setups=(
        Setup(
            "public",
            "GitHub public repository",
            "none",
            "Read an open-source repository without a token; GitHub allows 60 requests an hour "
            "this way, so large repositories need a token",
        ),
        Setup(
            "token",
            "GitHub private repository",
            "pat",
            "Sign in with a fine-grained or classic token that can read the repository; for "
            "GitHub Enterprise Server also set api_url and web_url",
        ),
    ),
)
