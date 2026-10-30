"""Request and response models of the connection, project and source-type routes.

The shapes follow the HTTP API spec's ``Connection``, ``SourceType``,
``ConnectionTest``, ``Ingestion`` and ``Project`` components.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from harborrag_app.api.schemas import ApiModel

SourceTypeName = Literal["jira", "confluence", "sharepoint", "local_file", "github"]
ConnectionStatusName = Literal["active", "needs_reauth", "error"]
ID_PATTERN = r"^[a-z0-9][a-z0-9_-]{0,62}$"


class SpecApiError(BaseModel):
    """The spec's error body."""

    code: str = Field(description="Stable code; clients branch on this, never on message")
    message: str = Field(description="Human-readable; wording may change")
    details: dict[str, Any] | list[dict[str, Any]] | None = Field(
        description="Extra context, e.g. the failing fields for 422"
    )
    trace_id: str | None = Field(description="Find this request in the logs and traces")


class SpecErrorResponse(BaseModel):
    """Every 4xx and 5xx response of these routes."""

    error: SpecApiError


class SecretReferenceIn(ApiModel):
    """Points a secret field at a value kept outside HarborRAG."""

    secret_ref: str = Field(min_length=1, description="env:NAME, or a path in the vault")


class SecretStatusOut(BaseModel):
    """What clients learn about a stored secret: never its value."""

    model_config = ConfigDict(populate_by_name=True)

    set: bool = Field(description="A value is stored")
    last4: str | None = Field(description="Last 4 characters, to tell keys apart")
    updated_at: datetime | None
    from_: Literal["stored", "env", "vault"] = Field(
        alias="from", serialization_alias="from", description="Where the value lives"
    )


CredentialValueIn = str | SecretReferenceIn | None
CredentialValueOut = str | SecretStatusOut | None


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
        description=(
            "Fields from the source type's auth schema. A secret field takes a string or a "
            "SecretRef; leave it out to keep the stored value, send null to clear it."
        )
    )
    config: dict[str, Any] = Field(
        description="What to sync, per the source type's config_schema; replaced as a whole"
    )
    sync_now: bool = Field(default=False, description="Start the first ingestion right away")


class ConnectionTestRequest(ApiModel):
    """``test`` body: form values to check before saving them."""

    source_type: SourceTypeName
    name: str | None = Field(default=None, min_length=1, max_length=255)
    credentials: dict[str, CredentialValueIn] = Field(default_factory=dict)
    config: dict[str, Any] = Field(default_factory=dict)


class SyncRequest(ApiModel):
    """``sync`` body."""

    mode: Literal["incremental", "full"] = Field(
        default="incremental",
        description=(
            "incremental reads items changed since the last successful sync; "
            "full re-reads everything"
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
    status: Literal["active", "needs_reauth", "error", "deleting"]
    last_ingestion: LastIngestionOut | None
    stats: ConnectionStatsOut
    created_by: str = Field(description="Read from the token")
    updated_by: str = Field(description="Read from the token")
    created_at: datetime
    updated_at: datetime


class CountersOut(BaseModel):
    discovered: int
    processed: int
    skipped: int
    failed: int


class TriggeredByOut(BaseModel):
    type: Literal["user", "api_key"]
    id: str


class LinksOut(BaseModel):
    temporal: str
    grafana: str
    langfuse: str


class IngestionOut(BaseModel):
    """One ingestion run."""

    id: str
    connection: str = Field(description="Connection ref")
    mode: Literal["incremental", "full"]
    status: Literal["queued", "running", "paused", "succeeded", "failed", "cancelled"]
    counters: CountersOut
    stages: list[dict[str, Any]]
    error_category: str | None
    triggered_by: TriggeredByOut
    settings_revision: str
    workflow_id: str
    run_id: str
    trace_id: str
    links: LinksOut
    created_at: datetime | None
    finished_at: datetime | None


class ConnectionPutOut(ConnectionOut):
    """A stored connection and, with ``sync_now``, the ingestion it started."""

    ingestion: IngestionOut | None = None


class ConnectionPage(BaseModel):
    items: list[ConnectionOut]
    next_cursor: str | None = Field(
        description="Pass as cursor to get the next page; null on the last page"
    )


class AuthMethodOut(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    id: Literal["api_token", "oauth", "pat", "app", "none"]
    title: str
    schema_: dict[str, Any] = Field(
        alias="schema",
        serialization_alias="schema",
        description="JSON Schema of the credential fields; secret fields have writeOnly: true",
    )


class UiHintOut(BaseModel):
    group: str
    order: int
    widget: Literal["text", "secret", "select", "textarea", "path"]
    help: str
    placeholder: str


class CapabilitiesOut(BaseModel):
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
    ui: dict[str, UiHintOut] = Field(description="Form hints per field name")
    capabilities: CapabilitiesOut


class SourceTypePage(BaseModel):
    items: list[SourceTypeOut]


class CheckOut(BaseModel):
    name: Literal["schema", "auth", "reachable", "permissions", "preview"]
    ok: bool
    message: str | None


class SampleItemOut(BaseModel):
    ref: str
    title: str


class PreviewOut(BaseModel):
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
    items: list[ProjectOut]
    next_cursor: str | None


class ConnectionChipOut(BaseModel):
    ref: str
    connection_id: str
    source_type: str
    name: str
    documents: int


class ConnectionChipPage(BaseModel):
    items: list[ConnectionChipOut]
