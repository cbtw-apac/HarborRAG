"""Write and resolve the secret fields of connection credentials.

Handles:
- ``CredentialSecretWriter``: keep, rotate, reference or clear each secret field
  of a PUT, storing new values through ``SecretsPort``
- ``SecretResolver``: one strategy per reference scheme, used by the activity
  that talks to the source; values never leave that call

Does NOT delete replaced secrets; the caller retires ``SecretWriteResult.retired``
only after the connection row commits.
"""

from __future__ import annotations

import hmac
import logging
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from harborrag_core.base import utc_now
from harborrag_core.contracts.errors import HarborConfigurationError, HarborNotFoundError
from harborrag_core.ports.secrets import SecretsPort
from harborrag_core.security.secret_refs import (
    REFERENCE_KEY,
    SecretRef,
    SecretSource,
    StoredSecret,
)
from harborrag_runtime.config.connectors.models import ConnectionValidationError, FieldIssue

logger = logging.getLogger("harborrag.runtime.connections.secrets")

OPERATOR_ONLY_HELP = (
    "env: references can be set only in connectors.yaml or with the harborrag CLI; "
    "send the value instead"
)


class SecretUnavailableError(HarborConfigurationError):
    """A referenced secret cannot be read where the connector runs."""

    error_code = "secret_unavailable"


class EnvSecretUnavailableError(SecretUnavailableError):
    """An ``env:`` reference names a variable the process does not have."""


@dataclass(frozen=True, slots=True)
class SecretWriteResult:
    """Credentials in stored form plus the stored refs a write created and replaced."""

    credentials: dict[str, Any]
    created: list[str] = field(default_factory=list)
    retired: list[str] = field(default_factory=list)


class SchemeResolver(Protocol):
    """Read the value behind one reference scheme."""

    @property
    def source(self) -> SecretSource:
        """The reference scheme this resolver reads."""

    async def resolve(self, ref: SecretRef, *, tenant_id: str) -> str:
        """Return the plaintext behind ``ref``."""


@dataclass(frozen=True, slots=True)
class StoredSecretResolver:
    """Resolve ``secret://`` references through ``SecretsPort``."""

    secrets: SecretsPort
    source: SecretSource = "stored"

    async def resolve(self, ref: SecretRef, *, tenant_id: str) -> str:
        """Return the stored value of ``ref`` for ``tenant_id``.

        Raises:
            SecretUnavailableError: The tenant has no such stored secret.
        """
        try:
            return await self.secrets.resolve(ref.value, tenant_id=tenant_id)
        except (HarborNotFoundError, KeyError) as error:
            raise SecretUnavailableError("a stored secret is missing") from error


@dataclass(frozen=True, slots=True)
class EnvironmentSecretResolver:
    """Resolve ``env:NAME`` references from the process that runs the connector.

    Only operator paths, ``connectors.yaml`` and the CLI, can store such a reference.
    """

    environ: Mapping[str, str] = field(default_factory=lambda: os.environ)
    source: SecretSource = "env"

    async def resolve(self, ref: SecretRef, *, tenant_id: str) -> str:
        """Return the variable ``ref`` names.

        Raises:
            EnvSecretUnavailableError: The variable is unset or empty.
        """
        value = self.environ.get(ref.target)
        if not value:
            raise EnvSecretUnavailableError(f"environment variable {ref.target} is not set")
        return value


class SecretResolver:
    """Dispatch each reference to the resolver of its scheme."""

    def __init__(self, resolvers: Sequence[SchemeResolver]) -> None:
        self._resolvers = {resolver.source: resolver for resolver in resolvers}

    @property
    def sources(self) -> frozenset[SecretSource]:
        """Reference schemes this resolver can read."""
        return frozenset(self._resolvers)

    async def resolve_credentials(
        self, credentials: Mapping[str, Any], *, tenant_id: str
    ) -> dict[str, str]:
        """Resolve every secret reference in ``credentials``.

        Args:
            credentials: Stored credentials of one connection.
            tenant_id: Tenant that owns the stored secrets.

        Returns:
            Plaintext per credential field held as a reference.

        Raises:
            SecretUnavailableError: A reference cannot be read here.
        """
        values: dict[str, str] = {}
        for name, value in credentials.items():
            stored = StoredSecret.from_json(value)
            if stored is None:
                continue
            resolver = self._resolvers.get(stored.ref.source)
            if resolver is None:
                raise SecretUnavailableError(f"{stored.ref.source} secrets are not configured")
            values[name] = await resolver.resolve(stored.ref, tenant_id=tenant_id)
        logger.debug("Resolved connection secrets fields=%s", sorted(values))
        return values


def default_secret_resolver(
    secrets: SecretsPort, environ: Mapping[str, str] | None = None
) -> SecretResolver:
    """Return the resolver chain every 2.0.0 install supports: stored and env."""
    return SecretResolver(
        [
            StoredSecretResolver(secrets),
            EnvironmentSecretResolver(os.environ if environ is None else environ),
        ]
    )


def reuses_stored_reference(value: Any, stored: Any) -> bool:
    """True when ``value`` is a ``{"secret_ref"}`` naming the reference ``stored`` holds.

    Such a value keeps the stored secret, exactly like leaving the field out.
    """
    previous = StoredSecret.from_json(stored)
    return (
        previous is not None
        and isinstance(value, Mapping)
        and value.get(REFERENCE_KEY) == previous.ref.value
    )


def reference_issue(
    value: Mapping[str, Any], stored: Any, sources: frozenset[SecretSource]
) -> str | None:
    """Return why the ``{"secret_ref"}`` ``value`` may not be written, or None when it may.

    A field may keep the reference it already holds. A new reference must parse,
    must not name a stored secret, which only a value creates, and must use a
    source in ``sources``: ``env:`` only from operator paths.
    """
    if reuses_stored_reference(value, stored):
        return None
    try:
        ref = SecretRef.parse(str(value[REFERENCE_KEY]))
    except ValueError as error:
        return str(error)
    if ref.source == "env" and "env" not in sources:
        return OPERATOR_ONLY_HELP
    if ref.is_stored or ref.source not in sources:
        return f"{ref.source} references are not supported here"
    return None


def reference_issues(
    incoming: Mapping[str, Any],
    existing: Mapping[str, Any] | None,
    *,
    sources: frozenset[SecretSource],
) -> list[FieldIssue]:
    """List the credential fields whose ``{"secret_ref"}`` the caller may not write."""
    stored = existing or {}
    issues: list[FieldIssue] = []
    for name, value in incoming.items():
        if not isinstance(value, Mapping) or not isinstance(value.get(REFERENCE_KEY), str):
            continue
        problem = reference_issue(value, stored.get(name), sources)
        if problem is not None:
            issues.append(FieldIssue(f"credentials.{name}", problem))
    return issues


class CredentialSecretWriter:
    """Apply a credentials write: secrets kept, rotated, referenced or cleared.

    Secret fields left out of ``incoming`` keep their stored value; a string
    equal to the stored value keeps it, any other string rotates it, a
    ``{"secret_ref"}`` points it elsewhere and ``None`` clears it. Non-secret
    fields are replaced by what ``incoming`` holds.
    """

    def __init__(
        self,
        secrets: SecretsPort,
        *,
        tenant_id: str,
        sources: frozenset[SecretSource] = frozenset({"stored", "env"}),
        now: datetime | None = None,
    ) -> None:
        self._secrets = secrets
        self._tenant_id = tenant_id
        self._sources = sources
        self._now = now or utc_now()

    async def apply(
        self,
        incoming: Mapping[str, Any],
        *,
        secret_fields: frozenset[str],
        existing: Mapping[str, Any] | None = None,
        keep: frozenset[str] | None = None,
    ) -> SecretWriteResult:
        """Merge ``incoming`` onto ``existing`` and store new secret values.

        Args:
            incoming: Credentials as the client sent them.
            secret_fields: Fields the source type marks as secrets.
            existing: Stored credentials, or ``None`` for a new connection.
            keep: Omitted secret fields that keep their stored value; ``None`` keeps every one.

        Returns:
            Stored-form credentials, refs created by this write (retire them if
            the row write fails) and refs it replaced (retire after commit).

        Raises:
            ConnectionValidationError: A secret field holds an unusable value.
        """
        stored_before = dict(existing or {})
        issues: list[FieldIssue] = []
        plan: dict[str, Any] = {}
        for name, value in incoming.items():
            if name not in secret_fields:
                plan[name] = value
            elif await self._holds(stored_before.get(name), value):
                plan[name] = stored_before[name]
            elif value is not None:
                plan[name] = self._classify(name, value, stored_before.get(name), issues)
        for name in secret_fields - set(incoming):
            if name in stored_before and (keep is None or name in keep):
                plan[name] = stored_before[name]
        if issues:
            raise ConnectionValidationError(issues)
        return await self._commit(plan, stored_before, secret_fields)

    async def _holds(self, before: Any, value: Any) -> bool:
        """True when ``value`` is the plaintext the stored secret ``before`` already holds."""
        stored = StoredSecret.from_json(before)
        if not isinstance(value, str) or not value or stored is None or not stored.ref.is_stored:
            return False
        try:
            current = await self._secrets.resolve(stored.ref.value, tenant_id=self._tenant_id)
        except Exception:  # noqa: BLE001 - an unreadable secret is replaced, not kept
            return False
        return hmac.compare_digest(current.encode(), value.encode())

    def _classify(self, name: str, value: Any, before: Any, issues: list[FieldIssue]) -> Any:
        """Validate one incoming secret value and return its planned stored form."""
        if isinstance(value, str):
            return value if value else self._issue(issues, name, "must not be empty")
        if isinstance(value, Mapping) and "set" in value and REFERENCE_KEY not in value:
            if before is None:
                return self._issue(issues, name, "has no stored value to keep")
            return before
        if not isinstance(value, Mapping) or not isinstance(value.get(REFERENCE_KEY), str):
            return self._issue(issues, name, "must be a string or a secret reference")
        if reuses_stored_reference(value, before):
            return before
        problem = reference_issue(value, before, self._sources)
        if problem is not None:
            return self._issue(issues, name, problem)
        return StoredSecret.for_reference(
            SecretRef.parse(value[REFERENCE_KEY]), self._now
        ).to_json()

    async def _commit(
        self,
        plan: Mapping[str, Any],
        stored_before: Mapping[str, Any],
        secret_fields: frozenset[str],
    ) -> SecretWriteResult:
        """Store planned plaintext values and work out which stored refs were replaced."""
        credentials: dict[str, Any] = {}
        created: list[str] = []
        try:
            for name, value in plan.items():
                if name in secret_fields and isinstance(value, str):
                    ref = await self._secrets.put(value, tenant_id=self._tenant_id)
                    created.append(ref)
                    value = StoredSecret.for_value(SecretRef(ref), value, self._now).to_json()
                credentials[name] = value
        except Exception:
            await self._discard(created)
            raise
        kept = {
            stored.ref.value
            for value in credentials.values()
            if (stored := StoredSecret.from_json(value)) is not None
        }
        retired = [
            stored.ref.value
            for value in stored_before.values()
            if (stored := StoredSecret.from_json(value)) is not None
            and stored.ref.is_stored
            and stored.ref.value not in kept
        ]
        return SecretWriteResult(credentials, created, retired)

    async def _discard(self, refs: list[str]) -> None:
        """Best-effort delete of refs stored by a write that then failed."""
        for ref in refs:
            try:
                await self._secrets.delete(ref, tenant_id=self._tenant_id)
            except Exception:
                logger.warning("Discarding an unused connection secret failed")

    @staticmethod
    def _issue(issues: list[FieldIssue], name: str, text: str) -> None:
        """Record a field issue for ``credentials.<name>``."""
        issues.append(FieldIssue(f"credentials.{name}", text))
