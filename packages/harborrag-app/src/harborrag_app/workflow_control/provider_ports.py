"""Provider CRUD, test-connection, routing, and cost port surface.

Split out of ports.py to keep that file under the repo's file-length gate;
mixed into BaseAppService alongside its other transport-neutral operations.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from harborrag_core.domain.provider import ProviderFamily

from .schemas import AppResponse


class ProviderPort:
    """Provider CRUD, test-connection, routing, and cost operations."""

    async def list_providers(
        self,
        *,
        tenant_ids: frozenset[str] | None,
        cursor: str | None = None,
        limit: int = 50,
    ) -> AppResponse:
        """One page of providers within ``tenant_ids``, walked via an opaque cursor;
        data={"providers": [...], "next_cursor": ...}, secret values never appear."""
        raise NotImplementedError

    async def get_provider(
        self, provider_id: str, *, tenant_ids: frozenset[str] | None
    ) -> AppResponse:
        """One provider by id within ``tenant_ids``; raises HarborNotFoundError when missing."""
        raise NotImplementedError

    async def create_provider(  # noqa: PLR0913 - mirrors create_source's explicit secret-handling fields
        self,
        *,
        tenant_id: str,
        name: str,
        family: ProviderFamily,
        config: Mapping[str, object],
        api_key: str | None,
        actor: str,
    ) -> AppResponse:
        """Create a provider; ``api_key`` (if given) is stored via the secrets port."""
        raise NotImplementedError

    async def update_provider(
        self,
        provider_id: str,
        *,
        updates: dict[str, object],
        actor: str,
        tenant_ids: frozenset[str] | None,
    ) -> AppResponse:
        """Apply a partial update within ``tenant_ids``; ``api_key`` rotates the stored secret."""
        raise NotImplementedError

    async def delete_provider(
        self, provider_id: str, *, actor: str, tenant_ids: frozenset[str] | None
    ) -> AppResponse:
        """Soft-delete a provider within ``tenant_ids`` and forget its stored secret, if any."""
        raise NotImplementedError

    async def test_provider_connection(
        self, provider_id: str, *, tenant_ids: frozenset[str] | None
    ) -> AppResponse:
        """Make one real call to a chat provider; HarborCapabilityError for other families."""
        raise NotImplementedError

    async def list_routing_rules(self) -> AppResponse:
        """Every routing rule (workspace-wide, not tenant-scoped)."""
        raise NotImplementedError

    async def replace_routing_rules(
        self, rules: Sequence[Mapping[str, object]], *, actor: str
    ) -> AppResponse:
        """Atomically replace the whole routing table with ``rules``."""
        raise NotImplementedError

    async def get_provider_cost(self, *, tenant_ids: frozenset[str] | None) -> AppResponse:
        """In-memory spend snapshot per provider within ``tenant_ids`` since process start."""
        raise NotImplementedError
