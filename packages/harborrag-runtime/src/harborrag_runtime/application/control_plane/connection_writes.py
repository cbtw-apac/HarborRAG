"""Rules for writing one connection: preconditions, secret handling, validation and the stored row.

Pure functions over a write and the stored connection; the service does the I/O around them.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from harborrag_core.contracts.errors import HarborImmutableFieldError, HarborPreconditionFailedError
from harborrag_core.domain.connection import Connection, ConnectionBuilder
from harborrag_core.security.secret_refs import SecretSource, all_refs
from harborrag_runtime.config.connectors.models import (
    ConnectionValidationError,
    FieldIssue,
    SourceTypeSpec,
    ValidatedConnection,
)
from harborrag_runtime.config.connectors.models.builder import validate_connection
from harborrag_runtime.config.connectors.models.spec import METHOD_KEY
from harborrag_runtime.connections import reuses_stored_reference
from harborrag_runtime.ingestion.connections import ConnectionUnavailableError

from .connection_models import ConnectionKey, ConnectionWrite, WriteOrigin


def check_preconditions(
    existing: Connection | None,
    spec: SourceTypeSpec,
    expected_version: int | None,
    create_only: bool,
) -> None:
    """Refuse a write that would break an immutable field or a precondition, before any I/O."""
    if existing is None:
        if expected_version is not None:
            raise HarborPreconditionFailedError("connection does not exist")
        return
    if existing.status == "deleting":
        raise ConnectionUnavailableError(f"connection {existing.ref!r} is being deleted")
    if existing.source_type != spec.source_type:
        raise HarborImmutableFieldError(
            "source_type cannot change; delete the connection or pick another connection_id",
            field="source_type",
        )
    if create_only:
        raise HarborPreconditionFailedError("connection already exists")
    if expected_version is not None and expected_version != existing.version:
        raise HarborPreconditionFailedError("connection version does not match")


def plan_credentials(
    incoming: Mapping[str, Any], existing: Connection | None, spec: SourceTypeSpec
) -> tuple[dict[str, Any], frozenset[str]]:
    """Return the sent credentials without ``SecretStatus`` echoes, and the stored secrets kept.

    A secret left out, echoed as its status, or sent back as the reference it
    already holds keeps its stored value only when it belongs to the auth method
    being written and no endpoint field changed.

    Raises:
        ConnectionValidationError: An endpoint changed while a stored secret would be kept.
    """
    sent = {name: value for name, value in incoming.items() if not _is_status_echo(value)}
    if existing is None:
        return sent, frozenset()
    try:
        secrets = spec.method_secret_fields(sent.get(METHOD_KEY))
    except KeyError:
        return sent, frozenset()
    keep = frozenset(name for name in secrets if name not in sent and name in existing.credentials)
    reused = frozenset(
        name
        for name in secrets
        if name in sent and reuses_stored_reference(sent[name], existing.credentials.get(name))
    )
    moved = sorted(
        name
        for name in spec.endpoints
        if _endpoint(sent.get(name)) != _endpoint(existing.credentials.get(name))
    )
    if (keep or reused) and moved:
        raise ConnectionValidationError(
            [
                FieldIssue(f"credentials.{name}", f"send it again: {', '.join(moved)} changed")
                for name in sorted(keep | reused)
            ]
        )
    return sent, keep


class ConcurrentWriteError(Exception):
    """A save without a precondition lost a race with another write."""


def refuse_operator_fields(
    spec: SourceTypeSpec, write: ConnectionWrite, existing: Connection | None, origin: WriteOrigin
) -> None:
    """Refuse an API write that only operators may make, such as setting a worker path.

    Raises:
        ConnectionValidationError: ``origin`` is ``api`` and an operator-only field
            changes or an API-only install rule refuses the config.
    """
    if origin != "api":
        return
    issues = [
        *spec.operator_field_issues(write.credentials, existing.credentials if existing else None),
        *spec.api_policy_issues(write.config),
    ]
    if issues:
        raise ConnectionValidationError(issues)


def secret_sources(origin: WriteOrigin) -> frozenset[SecretSource]:
    """Return the reference sources a caller may set: ``env:`` only from operator paths."""
    return frozenset({"stored", "env"}) if origin == "operator" else frozenset({"stored"})


def _is_status_echo(value: Any) -> bool:
    """Report whether ``value`` is a ``SecretStatus`` sent back as read."""
    return isinstance(value, Mapping) and "set" in value and "secret_ref" not in value


def _endpoint(value: Any) -> str | None:
    """Normalize an endpoint URL for comparison; blank counts as unset."""
    text = str(value).strip().rstrip("/").casefold() if value is not None else ""
    return text or None


def effective_credentials(
    sent: Mapping[str, Any], existing: Connection | None, keep: frozenset[str]
) -> dict[str, Any]:
    """Credentials as they will be once the write applies, for validation only."""
    kept = {name: existing.credentials[name] for name in keep} if existing else {}
    return {**kept, **{name: value for name, value in sent.items() if value is not None}}


def validate_write(
    spec: SourceTypeSpec,
    credentials: Mapping[str, Any],
    write: ConnectionWrite,
    existing: Connection | None,
    tenant_id: str,
) -> ValidatedConnection:
    """Check the write against the source type's schemas, then against its connector.

    Raises:
        ConnectionValidationError: A schema or the connector rejects the values.
    """
    return validate_connection(
        spec, credentials, write.config, write.effective_tuning(existing), tenant_id=tenant_id
    )


def build_connection(  # noqa: PLR0913 - the parts of one stored connection
    key: ConnectionKey,
    existing: Connection | None,
    spec: SourceTypeSpec,
    write: ConnectionWrite,
    credentials: dict[str, Any],
    validated: ValidatedConnection,
    actor: str,
) -> Connection:
    """Assemble the connection to store; a write that may fix it clears an attention status."""
    builder = (
        ConnectionBuilder.from_existing(existing)
        if existing
        else ConnectionBuilder.for_new(
            tenant_id=key.tenant_id, project_id=key.project_id, connection_id=key.connection_id
        )
    )
    builder.source_type(spec.source_type).name(write.name).credentials(credentials)
    builder.config(validated.config).tuning(validated.tuning)
    builder.secret_refs(all_refs(credentials)).actor(actor)
    if existing is not None and (
        existing.status == "error"
        or existing.credentials != credentials
        or (existing.status == "needs_reauth" and _sends_secret_value(write, spec))
    ):
        builder.status("active")
    return builder.build()


def _sends_secret_value(write: ConnectionWrite, spec: SourceTypeSpec) -> bool:
    """Report whether the write carries a secret as a value, not a status or reference."""
    return any(
        isinstance(write.credentials.get(name), str) and write.credentials.get(name)
        for name in spec.secret_fields
    )


def unchanged(before: Connection, after: Connection) -> bool:
    """Report whether a write would store exactly what ``before`` holds."""
    return (
        before.name == after.name
        and before.status == after.status
        and before.status_detail == after.status_detail
        and not changed_fields(before, after)
    )


def changed_fields(before: Connection, after: Connection) -> list[str]:
    """Names of fields a write changed, as ``name`` or ``<part>.<field>``."""
    changed = ["name"] if before.name != after.name else []
    for part, label in (
        ("credentials", "credentials"),
        ("config", "config"),
        ("tuning", "advanced"),
    ):
        old: Mapping[str, Any] = getattr(before, part)
        new: Mapping[str, Any] = getattr(after, part)
        changed.extend(
            f"{label}.{name}"
            for name in sorted(set(old) | set(new))
            if old.get(name) != new.get(name)
        )
    return changed


def merge_secrets(
    validated: Mapping[str, Any], secrets: Mapping[str, Any], secret_fields: frozenset[str]
) -> dict[str, Any]:
    """Merge the validated non-secret fields with the secret fields as ``secrets`` holds them."""
    merged = {name: value for name, value in validated.items() if name not in secret_fields}
    merged.update({name: value for name, value in secrets.items() if name in secret_fields})
    return merged
