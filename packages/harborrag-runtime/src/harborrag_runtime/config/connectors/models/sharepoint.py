"""SharePoint source type.

The auth method selects the dataclass ``auth_mode``: ``app`` is app-only with a
client secret, ``oauth`` is delegated device-code sign-in whose refresh token
lives in ``token_cache_path``, and ``pat`` is a pre-issued Graph token.
"""

from __future__ import annotations

from pydantic import Field

from .common import PagedHttpTuning
from .fields import HttpUrl, Secret, StrictModel, widget
from .spec import AuthMethodSpec, Capabilities, SourceTypeSpec

_GRAPH_HELP = "Microsoft Graph endpoint; change only for sovereign clouds"
_TENANT_HELP = "Directory (tenant) ID of the Entra ID app registration"
_CLIENT_HELP = "Application (client) ID of the Entra ID app registration"
_TENANT_EXAMPLE = "11111111-2222-3333-4444-555555555555"
_CLIENT_EXAMPLE = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


class SharePointAppCredentials(StrictModel):
    """App-only access with a client secret."""

    tenant_id: str = Field(min_length=1, description=_TENANT_HELP, examples=[_TENANT_EXAMPLE])
    client_id: str = Field(min_length=1, description=_CLIENT_HELP, examples=[_CLIENT_EXAMPLE])
    client_secret: Secret = Field(
        description="Client secret of the app registration", examples=["Q~8xr…"]
    )
    graph_api_url: HttpUrl | None = Field(default=None, description=_GRAPH_HELP)


class SharePointDelegatedCredentials(StrictModel):
    """Delegated access as the user who signed in with the device-code flow."""

    tenant_id: str = Field(min_length=1, description=_TENANT_HELP, examples=[_TENANT_EXAMPLE])
    client_id: str = Field(min_length=1, description=_CLIENT_HELP, examples=[_CLIENT_EXAMPLE])
    token_cache_path: str | None = Field(
        default=None,
        description="Where the worker keeps the refresh token; defaults under ~/.harborrag",
        json_schema_extra=widget("path"),
    )
    graph_api_url: HttpUrl | None = Field(default=None, description=_GRAPH_HELP)


class SharePointTokenCredentials(StrictModel):
    """A pre-issued Microsoft Graph access token."""

    access_token: Secret = Field(description="Microsoft Graph bearer token", examples=["eyJ0eXAi…"])
    graph_api_url: HttpUrl | None = Field(default=None, description=_GRAPH_HELP)


class SharePointConfig(StrictModel):
    """What to sync from SharePoint."""

    site_url: HttpUrl | None = Field(
        default=None,
        description="Site to crawl",
        examples=["https://acme.sharepoint.com/sites/knowledge"],
    )
    hostname: str | None = Field(default=None, description="Site host, when not using site_url")
    site_path: str | None = Field(default=None, description="Site path, when not using site_url")
    site_id: str | None = Field(default=None, description="Graph site ID")
    drive_id: str | None = Field(default=None, description="Document library ID")
    drive_name: str | None = Field(default=None, description="Document library name")
    root_path: str | None = Field(default=None, description="Folder inside the library")
    item_ids: list[str] | None = Field(default=None, description="Explicit files to sync")
    crawl_all_drives: bool = Field(default=False, description="Walk every document library")
    recursive: bool = Field(default=True, description="Descend into subfolders")
    allowed_tenant_ids: list[str] | None = Field(default=None)
    allowed_sites: list[str] | None = Field(default=None)
    allowed_path_prefixes: list[str] | None = Field(default=None)
    allowed_extensions: list[str] | None = Field(default=None, description="File types to keep")
    excluded_extensions: list[str] = Field(default_factory=list, description="File types to drop")
    include_hidden: bool = Field(default=False, description="Include hidden files")
    include_pages: bool = Field(default=False, description="Also index the site's modern pages")
    max_file_size_bytes: int | None = Field(default=None, ge=0)
    fail_on_error: bool = Field(default=False, description="Stop the run on the first item error")


SHAREPOINT = SourceTypeSpec(
    source_type="sharepoint",
    name="SharePoint",
    category="files",
    provider="sharepoint",
    auth_methods=(
        AuthMethodSpec(
            "app", "App registration", SharePointAppCredentials, {"auth_mode": "client_credentials"}
        ),
        AuthMethodSpec(
            "oauth",
            "Device-code sign-in",
            SharePointDelegatedCredentials,
            {"auth_mode": "device_code"},
        ),
        AuthMethodSpec(
            "pat", "Access token", SharePointTokenCredentials, {"auth_mode": "access_token"}
        ),
    ),
    config_model=SharePointConfig,
    tuning_model=PagedHttpTuning,
    capabilities=Capabilities(incremental=True, attachments=False),
    aliases=("microsoft_sharepoint", "sharepoint_online"),
)
