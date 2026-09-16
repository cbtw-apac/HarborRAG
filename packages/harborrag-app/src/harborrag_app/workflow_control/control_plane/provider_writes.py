"""Control-plane write use cases for providers, test-connection, and routing.

Split out of writes.py to keep that file under the repo's file-length gate;
mixed into AppService alongside ControlPlaneWritesMixin, which supplies the
concrete _control_plane().
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any
from uuid import uuid4

from harborrag_core.contracts.errors import (
    HarborCapabilityError,
    HarborNotFoundError,
    HarborValidationError,
)
from harborrag_core.domain.activity import ActivityEntry
from harborrag_core.domain.provider import Provider, ProviderFamily
from harborrag_core.domain.routing_rule import RoutingRule
from harborrag_core.domain.validation import require_tenant_id, validate_secret_free_config
from harborrag_runtime.composition import ControlPlaneRepositories

from ..schemas import AppResponse
from .effect_recovery import log_activity as _log_activity
from .effect_recovery import retire_refs as _retire_refs

_MUTABLE_PROVIDER_FIELDS = frozenset({"name", "config", "api_key"})
_WORKSPACE_TENANT_ID = "DEFAULT"


class ControlPlaneProviderWritesMixin:
    """Provider/routing write-side control-plane use cases shared by AppService."""

    def _control_plane(self) -> ControlPlaneRepositories:
        raise NotImplementedError

    async def create_provider(  # noqa: PLR0913 - explicit provider-creation fields keep secret handling auditable
        self,
        *,
        tenant_id: str,
        name: str,
        family: ProviderFamily,
        config: Mapping[str, Any],
        api_key: str | None,
        actor: str,
    ) -> AppResponse:
        """Create a provider; ``api_key`` (if given) is stored via the secrets port."""
        control_plane = self._control_plane()
        validated_config = _validated_provider_config(config)
        try:
            require_tenant_id(tenant_id)
        except ValueError as exc:
            raise HarborValidationError(str(exc)) from exc
        # Validated above, before secrets.put(): a bad tenant_id must fail here,
        # not after a secret is already stored with nothing to retire it.
        secret_ref = await control_plane.secrets.put(api_key) if api_key else None
        provider = Provider(
            id=f"prov_{uuid4().hex}",
            tenant_id=tenant_id,
            name=name,
            family=family,
            config=validated_config,
            secret_ref=secret_ref,
        )
        try:
            created = await control_plane.providers.save(provider)
        except Exception:
            if secret_ref is not None:
                await _retire_refs(control_plane, [secret_ref])
            raise
        await _log_activity(
            control_plane,
            ActivityEntry(
                id=f"act_{uuid4().hex}",
                tenant_id=tenant_id,
                actor=actor,
                verb="created",
                entity_type="provider",
                entity_id=created.id,
                summary=f"Created provider {created.name!r}",
            ),
        )
        return AppResponse(True, {"provider": created})

    async def update_provider(
        self,
        provider_id: str,
        *,
        updates: dict[str, Any],
        actor: str,
        tenant_ids: frozenset[str] | None,
    ) -> AppResponse:
        """Apply a partial update within ``tenant_ids``; ``api_key`` rotates the stored secret."""
        unknown = set(updates) - _MUTABLE_PROVIDER_FIELDS
        if unknown:
            raise HarborValidationError(f"unsupported provider fields: {sorted(unknown)}")
        control_plane = self._control_plane()
        provider = await control_plane.providers.get(provider_id, tenant_ids=tenant_ids)
        if provider is None:
            raise HarborNotFoundError(f"provider {provider_id!r} not found")
        if "name" in updates:
            provider.name = updates["name"]
        if "config" in updates:
            provider.config = _validated_provider_config(updates["config"])
        newly_put_ref: str | None = None
        stale_ref: str | None = None
        if "api_key" in updates:
            new_key = updates["api_key"]
            stale_ref = provider.secret_ref
            newly_put_ref = await control_plane.secrets.put(new_key) if new_key else None
            provider.secret_ref = newly_put_ref
        try:
            updated = await control_plane.providers.save(provider)
        except Exception:
            # The provider row never picked up the new ref -- retire it so it
            # doesn't linger as an orphaned secret pointing at nothing.
            if newly_put_ref is not None:
                await _retire_refs(control_plane, [newly_put_ref])
            raise
        # Only now that the provider row durably references the new ref is it
        # safe to retire the old one.
        if stale_ref is not None:
            await _retire_refs(control_plane, [stale_ref])
        await _log_activity(
            control_plane,
            ActivityEntry(
                id=f"act_{uuid4().hex}",
                tenant_id=updated.tenant_id,
                actor=actor,
                verb="updated",
                entity_type="provider",
                entity_id=updated.id,
                summary=f"Updated provider {updated.name!r}",
            ),
        )
        return AppResponse(True, {"provider": updated})

    async def delete_provider(
        self, provider_id: str, *, actor: str, tenant_ids: frozenset[str] | None
    ) -> AppResponse:
        """Soft-delete a provider within ``tenant_ids`` and forget its stored secret, if any.

        The provider row is tombstoned, not removed (``ProviderRepositoryPort``'s
        docstring) -- a routing rule may still reference its id via a DB
        foreign key. The secret is genuinely retired either way: only the
        row, and the fact that it once held a live credential, survives.
        """
        control_plane = self._control_plane()
        provider = await control_plane.providers.get(provider_id, tenant_ids=tenant_ids)
        if provider is None:
            raise HarborNotFoundError(f"provider {provider_id!r} not found")
        await control_plane.providers.delete(provider_id, tenant_ids=tenant_ids)
        if provider.secret_ref is not None:
            await _retire_refs(control_plane, [provider.secret_ref])
        await _log_activity(
            control_plane,
            ActivityEntry(
                id=f"act_{uuid4().hex}",
                tenant_id=provider.tenant_id,
                actor=actor,
                verb="deleted",
                entity_type="provider",
                entity_id=provider_id,
                summary=f"Deleted provider {provider.name!r}",
            ),
        )
        return AppResponse(True, {"provider_id": provider_id})

    async def test_provider_connection(
        self, provider_id: str, *, tenant_ids: frozenset[str] | None
    ) -> AppResponse:
        """Make one real call to a chat provider; HarborCapabilityError for other families.

        Only ``provider`` (never its raw secret) crosses this boundary -- the
        configured ``ProviderProbePort`` is the sole component allowed to
        resolve ``provider.secret_ref`` (see ``ProviderProbePort``'s docstring).
        """
        control_plane = self._control_plane()
        provider = await control_plane.providers.get(provider_id, tenant_ids=tenant_ids)
        if provider is None:
            raise HarborNotFoundError(f"provider {provider_id!r} not found")
        if provider.family != "chat":
            raise HarborCapabilityError(
                f"test-connection is only supported for chat providers, not {provider.family!r}"
            )
        result = await control_plane.provider_probe.probe(provider)
        return AppResponse(True, {"result": result})

    async def replace_routing_rules(
        self, rules: Sequence[Mapping[str, Any]], *, actor: str
    ) -> AppResponse:
        """Atomically replace the whole routing table with ``rules``.

        Not tenant-scoped (see ``RoutingRuleRepositoryPort``'s docstring), so
        every referenced provider is looked up unrestricted; a rule naming a
        provider that doesn't exist anywhere fails the whole replace with a
        clean 404 instead of a foreign-key database error.
        """
        control_plane = self._control_plane()
        domain_rules: list[RoutingRule] = []
        for entry in rules:
            provider_id = entry["provider_id"]
            if await control_plane.providers.get(provider_id, tenant_ids=None) is None:
                raise HarborNotFoundError(f"provider {provider_id!r} not found")
            try:
                domain_rules.append(
                    RoutingRule(
                        id=f"rule_{uuid4().hex}",
                        family=entry["family"],
                        provider_id=provider_id,
                        priority=int(entry.get("priority", 0)),
                    )
                )
            except (ValueError, TypeError) as exc:
                raise HarborValidationError(
                    f"invalid routing rule for {provider_id!r}: {exc}"
                ) from exc
        replaced = await control_plane.routing_rules.replace(domain_rules)
        await _log_activity(
            control_plane,
            ActivityEntry(
                id=f"act_{uuid4().hex}",
                tenant_id=_WORKSPACE_TENANT_ID,
                actor=actor,
                verb="replaced",
                entity_type="routing_rules",
                entity_id="routing",
                summary=f"Replaced routing rules ({len(replaced)} rule(s))",
            ),
        )
        return AppResponse(True, {"rules": replaced})


def _validated_provider_config(config: Mapping[str, Any]) -> dict[str, Any]:
    """Copy ``config``, rejecting any raw secret-shaped value with a clean 422.

    Unlike a source's config, a provider's config has no per-field secret
    extraction -- callers use the dedicated ``api_key`` field for that, so a
    sensitive-looking key here (e.g. ``config={"api_key": "raw"}``) is always
    a caller mistake, not a value this layer should silently store or scrub.
    """
    try:
        validate_secret_free_config(config)
    except ValueError as exc:
        raise HarborValidationError(str(exc)) from exc
    return dict(config)
