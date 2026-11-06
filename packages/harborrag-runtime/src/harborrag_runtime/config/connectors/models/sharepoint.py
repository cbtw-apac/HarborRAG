"""SharePoint source type.

The auth method selects the dataclass ``auth_mode``: ``app`` is app-only with a
client secret, ``oauth`` is delegated device-code sign-in whose refresh token
lives in ``token_cache_path``, and ``pat`` is a pre-issued Graph token.
"""

from __future__ import annotations

from functools import cache
from typing import Self

from pydantic import ConfigDict, Field, model_validator

from harborrag_adapters.connectors.file_filters import normalize_extension

from .common import PagedHttpTuning
from .fields import HttpsUrl, Secret, StrictModel, widget
from .spec import AuthMethodSpec, Capabilities, Setup, SourceTypeSpec

_GRAPH_HELP = (
    "Microsoft Graph endpoint; left out, https://graph.microsoft.com/v1.0. "
    "Change only for sovereign clouds"
)
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
    graph_api_url: HttpsUrl | None = Field(default=None, description=_GRAPH_HELP)


class SharePointDelegatedCredentials(StrictModel):
    """Delegated access as the user who signed in with the device-code flow."""

    tenant_id: str = Field(min_length=1, description=_TENANT_HELP, examples=[_TENANT_EXAMPLE])
    client_id: str = Field(min_length=1, description=_CLIENT_HELP, examples=[_CLIENT_EXAMPLE])
    token_cache_path: str | None = Field(
        default=None,
        description=(
            "Where the worker keeps the refresh token; defaults under ~/.harborrag. "
            "Set only in connectors.yaml or with the harborrag CLI"
        ),
        json_schema_extra=widget("path"),
    )
    graph_api_url: HttpsUrl | None = Field(default=None, description=_GRAPH_HELP)


class SharePointTokenCredentials(StrictModel):
    """A pre-issued Microsoft Graph access token."""

    access_token: Secret = Field(description="Microsoft Graph bearer token", examples=["eyJ0eXAi…"])
    graph_api_url: HttpsUrl | None = Field(default=None, description=_GRAPH_HELP)


class SharePointConfig(StrictModel):
    """What to sync from SharePoint: one site by ``site_url``, ``site_id`` or ``hostname`` and ``site_path``."""

    model_config = ConfigDict(
        json_schema_extra={
            "anyOf": [
                {"required": ["site_url"]},
                {"required": ["site_id"]},
                {"required": ["hostname", "site_path"]},
            ]
        }
    )

    site_url: HttpsUrl | None = Field(
        default=None,
        description="Site to crawl",
        examples=["https://acme.sharepoint.com/sites/knowledge"],
    )
    hostname: str | None = Field(
        default=None, description="Site host, with site_path, when not using site_url"
    )
    site_path: str | None = Field(
        default=None, description="Site path, such as /sites/knowledge, with hostname"
    )
    site_id: str | None = Field(default=None, description="Graph site ID, instead of site_url")
    drive_id: str | None = Field(
        default=None, description="Document library ID; left out, the site's default library"
    )
    drive_name: str | None = Field(
        default=None, description="Document library name, instead of drive_id"
    )
    root_path: str | None = Field(
        default=None, description="Folder inside the library; not with item_ids"
    )
    item_ids: list[str] | None = Field(
        default=None, description="Explicit files to sync; not with root_path"
    )
    crawl_all_drives: bool = Field(default=False, description="Walk every document library")
    recursive: bool = Field(default=True, description="Descend into subfolders")
    allowed_tenant_ids: list[str] | None = Field(
        default=None, description="Entra tenant IDs the site must belong to"
    )
    allowed_sites: list[str] | None = Field(
        default=None, description="Graph site IDs the connection may read"
    )
    allowed_path_prefixes: list[str] | None = Field(
        default=None, description="Folder prefixes the connection may read"
    )
    allowed_extensions: list[str] | None = Field(
        default=None,
        description="File types to keep, such as pdf; left out, common document and image types",
    )
    excluded_extensions: list[str] = Field(default_factory=list, description="File types to drop")
    include_hidden: bool = Field(default=False, description="Include hidden files")
    include_pages: bool = Field(default=False, description="Also index the site's modern pages")
    max_file_size_bytes: int | None = Field(
        default=None, ge=0, description="Largest file to read, in bytes; left out, 20 MiB"
    )
    fail_on_error: bool = Field(default=False, description="Stop the run on the first item error")

    @model_validator(mode="after")
    def _one_site(self) -> Self:
        """Require a site: ``site_url``, ``site_id`` or ``hostname`` with ``site_path``."""
        if not (self.site_url or self.site_id or (self.hostname and self.site_path)):
            raise ValueError("set site_url, site_id, or hostname and site_path")
        return self

    @model_validator(mode="after")
    def _parseable_extensions(self) -> Self:
        """Refuse an ``allowed_extensions`` entry no default parser family can read."""
        allowed = {normalize_extension(value) for value in self.allowed_extensions or ()}
        unknown = sorted(allowed - _parseable_extensions())
        if unknown:
            raise ValueError(f"allowed_extensions has unsupported extensions: {', '.join(unknown)}")
        return self


@cache
def _parseable_extensions() -> frozenset[str]:
    """Every file extension the default parser families can read."""
    from harborrag_adapters.parsers.factory import HarborParserFactory

    registry = HarborParserFactory().create_registry()
    return frozenset(
        extension.lower() for parser in registry.families() for extension in parser.extensions
    )


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
    endpoints=frozenset({"graph_api_url"}),
    operator_fields=frozenset({"token_cache_path"}),
    tenant_keyword="harborrag_tenant_id",
    aliases=("microsoft_sharepoint", "sharepoint_online"),
    setups=(
        Setup(
            "app",
            "SharePoint app registration",
            "app",
            "Sign in as an Entra ID app with its client secret; the app needs the application "
            "permission Sites.Selected granted on the site, and broader roles such as "
            "Sites.Read.All are refused",
        ),
        Setup(
            "device_code",
            "SharePoint device-code sign-in",
            "oauth",
            "Read as a user who signed in once with python -m "
            "harborrag_adapters.connectors.sharepoint.login on a host whose token_cache_path "
            "the workers can read; the app needs delegated Sites.Selected and offline_access "
            "and public client flows enabled",
        ),
    ),
)
