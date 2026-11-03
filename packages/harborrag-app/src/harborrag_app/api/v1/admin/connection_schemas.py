"""Request and response models of the connection, project and source-type routes.

The shapes follow the HTTP API spec's ``Connection``, ``SourceType``,
``ConnectionTest``, ``Ingestion`` and ``Project`` components.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, WithJsonSchema
from pydantic.json_schema import JsonDict

from harborrag_app.api.schemas import ApiError, ApiModel

SourceTypeName = Literal["jira", "confluence", "sharepoint", "local_file", "github"]
ConnectionStatusName = Literal["active", "needs_reauth", "error"]
ID_PATTERN = r"^[a-z0-9][a-z0-9_-]{0,62}$"


class SecretStatusOut(BaseModel):
    """What clients learn about a stored secret: never its value."""

    model_config = ConfigDict(populate_by_name=True)

    set: bool = Field(description="A value is stored")
    last4: str | None = Field(description="Last 4 characters, to tell keys apart")
    updated_at: datetime | None
    from_: Literal["stored", "env", "vault"] = Field(
        alias="from", serialization_alias="from", description="Where the value lives"
    )


CredentialValueIn = Annotated[
    Any,
    WithJsonSchema(
        {
            "anyOf": [
                {"type": "string"},
                {
                    "type": "object",
                    "title": "SecretRef",
                    "properties": {
                        "secret_ref": {
                            "type": "string",
                            "minLength": 1,
                            "description": (
                                "The reference the field already holds, as read; a new env:NAME "
                                "can be set only in connectors.yaml or with the harborrag CLI"
                            ),
                        }
                    },
                    "required": ["secret_ref"],
                },
                {
                    "type": "object",
                    "title": "SecretStatus",
                    "description": "A SecretStatus sent back as read; keeps the stored value",
                    "properties": {"set": {"type": "boolean"}},
                    "required": ["set"],
                },
                {"type": "null"},
            ]
        }
    ),
]
CredentialValueOut = str | SecretStatusOut | None

_CREDENTIALS_HELP = (
    "Fields from the source type's auth schema. When the type has several auth methods, "
    "method names the one these fields belong to; left out, the first method. A secret "
    "field takes the secret as a string; leave it out, or send back its SecretStatus or "
    "SecretRef, to keep the stored value, and send null to clear it. A stored secret is "
    "kept only while the method and its endpoint fields (base_url, api_url, web_url, "
    "graph_api_url) stay the same."
)
_CREDENTIALS_SCHEMA: JsonDict = {
    "properties": {
        "method": {
            "type": "string",
            "enum": ["api_token", "oauth", "pat", "app", "none"],
            "description": "Auth method, when the type has several",
            "examples": ["api_token"],
        }
    }
}


class ConnectionWriteRequest(ApiModel):
    """``PUT`` body: everything about one connection, config replaced as a whole."""

    source_type: SourceTypeName = Field(description="Required on create; can't change later")
    name: str | None = Field(
        default=None,
        min_length=1,
        max_length=255,
        description="Display name; default: connection_id",
    )
    credentials: dict[str, CredentialValueIn] = Field(
        description=_CREDENTIALS_HELP, json_schema_extra=_CREDENTIALS_SCHEMA
    )
    config: dict[str, Any] = Field(
        description="What to sync, per the source type's config_schema; replaced as a whole"
    )
    advanced: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Request rate, paging, timeout and retries per the source type's advanced_schema, "
            "replaced as a whole; leave it out to keep the stored values, a null field uses "
            "the connector default"
        ),
    )
    sync_now: bool = Field(default=False, description="Start the first ingestion right away")


class ConnectionTestRequest(ApiModel):
    """``test`` body: form values to check before saving them."""

    source_type: SourceTypeName
    name: str | None = Field(default=None, min_length=1, max_length=255)
    credentials: dict[str, CredentialValueIn] = Field(
        default_factory=dict, description=_CREDENTIALS_HELP, json_schema_extra=_CREDENTIALS_SCHEMA
    )
    config: dict[str, Any] = Field(
        default_factory=dict, description="What to sync, per the source type's config_schema"
    )
    advanced: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Request rate, paging, timeout and retries per the source type's advanced_schema, "
            "replaced as a whole; leave it out to keep the stored values, a null field uses "
            "the connector default"
        ),
    )


class SyncRequest(ApiModel):
    """``sync`` body."""

    mode: Literal["incremental", "force"] = Field(
        default="incremental",
        description=(
            "incremental reads items changed since the last successful sync; "
            "force re-reads and re-processes every item"
        ),
    )
    item_refs: list[str] = Field(
        default_factory=list, max_length=500, description="Limit the run to these items"
    )
    updated_after: datetime | None = Field(
        default=None, description="Re-read items changed since then, e.g. to backfill a window"
    )


class ProjectWriteRequest(ApiModel):
    """``PUT`` body of a project."""

    name: str = Field(min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=2000)


class LastIngestionOut(BaseModel):
    """The connection's most recent run."""

    id: str
    status: str
    finished_at: datetime | None


class ConnectionStatsOut(BaseModel):
    """Counts the stores answer cheaply; null where they cannot."""

    documents: int | None
    chunks: int | None
    entities: int | None
    errors: int | None


class ConnectionOut(BaseModel):
    """One configured data source."""

    tenant_id: str
    project_id: str
    connection_id: str = Field(pattern=ID_PATTERN)
    source_type: str
    ref: str = Field(description="project_id/connection_id; used in filters, documents and keys")
    name: str
    credentials: dict[str, CredentialValueOut] = Field(
        description="Fields from the source type's auth schema. Secret fields read back as SecretStatus."
    )
    config: dict[str, Any] = Field(description="What to sync, per the source type's config_schema")
    advanced: dict[str, Any] = Field(
        description="Request settings per the advanced_schema; a missing field uses the default"
    )
    status: Literal["active", "needs_reauth", "error", "deleting"]
    last_ingestion: LastIngestionOut | None
    stats: ConnectionStatsOut
    created_by: str = Field(description="Read from the token")
    updated_by: str = Field(description="Read from the token")
    created_at: datetime
    updated_at: datetime


class CountersOut(BaseModel):
    """Items an ingestion found, processed, skipped and failed."""

    discovered: int
    processed: int
    skipped: int
    failed: int


class TriggeredByOut(BaseModel):
    """Who started an ingestion, read from the credential."""

    type: Literal["user", "api_key"]
    id: str


class LinksOut(BaseModel):
    """Where to follow an ingestion in Temporal, Grafana and Langfuse."""

    temporal: str
    grafana: str
    langfuse: str


class IngestionOut(BaseModel):
    """One ingestion run."""

    id: str
    connection: str = Field(description="Connection ref")
    mode: Literal["incremental", "force"]
    status: Literal["queued", "running", "paused", "succeeded", "failed", "cancelled"]
    counters: CountersOut
    stages: list[dict[str, Any]]
    error_category: str | None
    triggered_by: TriggeredByOut
    settings_revision: str = Field(
        description="Fingerprint of the connection snapshot the run reads; empty before it starts"
    )
    workflow_id: str
    run_id: str
    trace_id: str
    links: LinksOut
    created_at: datetime | None
    finished_at: datetime | None


class ConnectionPutOut(ConnectionOut):
    """A stored connection and, with ``sync_now``, the ingestion it started."""

    ingestion: IngestionOut | None = None
    sync_error: ApiError | None = Field(
        default=None,
        description=(
            "Why sync_now started no ingestion, e.g. already_running; the connection was "
            "saved all the same"
        ),
    )


class ConnectionPage(BaseModel):
    """One page of connections, the ones needing attention first."""

    items: list[ConnectionOut]
    next_cursor: str | None = Field(
        description="Pass as cursor to get the next page; null on the last page"
    )


class UiHintOut(BaseModel):
    """How a form renders one field."""

    group: str
    order: int
    widget: Literal["text", "secret", "select", "textarea", "path", "list", "toggle", "number"]
    help: str
    placeholder: str


class AuthMethodOut(BaseModel):
    """One way to sign in to a source type, with its credential fields."""

    model_config = ConfigDict(populate_by_name=True)

    id: Literal["api_token", "oauth", "pat", "app", "none"]
    title: str
    schema_: dict[str, Any] = Field(
        alias="schema",
        serialization_alias="schema",
        description=(
            "JSON Schema of the credential fields; secret fields have writeOnly: true. When the "
            "type has several methods, the method property names this one"
        ),
    )
    ui: dict[str, UiHintOut] = Field(description="Form hints of this method's credential fields")


class CapabilitiesOut(BaseModel):
    """What a source type supports."""

    incremental: bool
    acl_sync: bool = Field(description="v2.1")
    attachments: bool


class SourceTypeOut(BaseModel):
    """A data source type with its credential and config schemas and form hints."""

    source_type: str
    name: str
    category: Literal["issue_tracker", "wiki", "files", "code"]
    icon: str
    version: str
    docs_url: str
    auth_methods: list[AuthMethodOut]
    config_schema: dict[str, Any] = Field(description="JSON Schema of what to sync")
    advanced_schema: dict[str, Any] = Field(
        description="JSON Schema of the request rate, paging, timeout and retry settings"
    )
    ui: dict[str, UiHintOut] = Field(
        description=(
            "Form hints of the config and advanced fields, and of credential fields that read "
            "the same in every auth method; auth_methods[].ui has each method's own"
        )
    )
    capabilities: CapabilitiesOut


class SourceTypePage(BaseModel):
    """Every installed source type."""

    items: list[SourceTypeOut]


class CheckOut(BaseModel):
    """One connection-test check and why it failed, if it did."""

    name: Literal["schema", "auth", "reachable", "permissions", "preview"]
    ok: bool
    message: str | None


class SampleItemOut(BaseModel):
    """An item the source would ingest."""

    ref: str
    title: str


class PreviewOut(BaseModel):
    """How many items the source holds, when it can tell, and a few of them."""

    estimated_items: int | None
    sample: list[SampleItemOut] = Field(description="Up to 5 items")


class ConnectionTestOut(BaseModel):
    """The checks run against a connection; nothing is stored."""

    ok: bool
    checks: list[CheckOut]
    error_category: (
        Literal["auth_invalid", "auth_expired", "forbidden", "unreachable", "rate_limited"] | None
    )
    preview: PreviewOut
    latency_ms: int


class JobAcceptedOut(BaseModel):
    """A removal that runs in the background."""

    job_id: str


class ProjectOut(BaseModel):
    """A project inside the tenant."""

    id: str = Field(pattern=ID_PATTERN, description="Chosen by you; unique in the tenant")
    tenant_id: str
    name: str
    description: str | None
    connection_count: int
    document_count: int
    created_at: datetime
    updated_at: datetime


class ProjectPage(BaseModel):
    """One page of projects."""

    items: list[ProjectOut]
    next_cursor: str | None


class ConnectionChipOut(BaseModel):
    """A connection a reader can search, for filter chips."""

    ref: str
    connection_id: str
    source_type: str
    name: str
    documents: int


class ConnectionChipPage(BaseModel):
    """The connections a reader can search in a project."""

    items: list[ConnectionChipOut]
