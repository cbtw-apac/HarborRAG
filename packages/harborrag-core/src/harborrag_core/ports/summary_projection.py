"""Provider-neutral persistence boundaries for semantic navigation projections."""

from collections.abc import Mapping
from typing import Protocol

from harborrag_core.ingestion import GraphNodeRecord
from harborrag_core.security.context import AccessContext
from harborrag_core.summaries import (
    MissingSourceDocument,
    SummaryBinding,
    SummaryCard,
    SummaryLease,
    SummarySnapshot,
)
from harborrag_core.summary_cards import SummaryView
from harborrag_core.topology.budget import BudgetAdmission, BudgetRequest


class SummaryCachePort(Protocol):
    async def get_card(self, tenant_id: str, key: str) -> SummaryCard | None: ...

    async def put_card(self, tenant_id: str, key: str, card: SummaryCard) -> SummaryCard:
        """Atomically select one immutable winner for a generation key."""
        ...


class SummaryReaderPort(Protocol):
    async def views(
        self,
        tenant_id: str,
        node_keys: tuple[str, ...],
        *,
        access: AccessContext,
        source_scopes: dict[str, str] | None = None,
    ) -> dict[str, SummaryView]: ...

    async def entity_evidence(
        self,
        tenant_id: str,
        node_keys: tuple[str, ...],
        *,
        access: AccessContext,
    ) -> dict[str, tuple[str, ...]]:
        """Return the chunks behind each readable source-entity summary.

        A summary point in the vector index is only a pointer: nothing may be
        served from it until the binding behind it passes the same permission and
        freshness checks ``views`` applies. This is that check, returning evidence
        identities rather than presentation text.
        """
        ...


class SummaryInputRepositoryPort(Protocol):
    """Read the canonical source material used to plan summaries."""

    async def source_documents(
        self, tenant_id: str, source_scope_id: str
    ) -> tuple[Mapping[str, object], ...]: ...

    async def retained_nodes(
        self, tenant_id: str, source_scope_id: str
    ) -> tuple[GraphNodeRecord, ...]: ...


class SummaryExecutionRepositoryPort(SummaryCachePort, Protocol):
    """Lease work and atomically publish summary products."""

    async def ingestion_active(self, tenant_id: str, source_scope_id: str) -> bool:
        """Whether an ingestion task for the scope is still pending or running.

        Entity cards compose every document under the entity, so writing them while
        documents are still arriving buys a card that the next publish supersedes.
        Leaf cards are per-version and immutable, so they are worth building anyway.
        """
        ...

    async def missing_documents(
        self, tenant_id: str, source_scope_id: str
    ) -> tuple[MissingSourceDocument, ...]:
        """Report discovered source items with no active published version.

        Coverage is decided while the run holds the lease, beside the snapshot it
        is writing cards against, so it belongs with execution rather than with
        the canonical artifact reads.
        """
        ...

    async def claim(
        self,
        tenant_id: str,
        *,
        lease_seconds: int = 300,
        source_scope_id: str | None = None,
    ) -> SummaryLease | None: ...

    async def renew(self, lease: SummaryLease, *, lease_seconds: int = 300) -> None: ...

    async def reserve(self, lease: SummaryLease, request: BudgetRequest) -> BudgetAdmission: ...

    async def finish(
        self,
        lease: SummaryLease,
        *,
        error_code: str | None = None,
        blocked: bool = False,
    ) -> bool: ...

    async def scope_current(self, lease: SummaryLease) -> bool: ...

    async def snapshot(self, lease: SummaryLease) -> SummarySnapshot: ...

    async def accept(
        self,
        lease: SummaryLease,
        snapshot: SummarySnapshot,
        binding: SummaryBinding,
        node: GraphNodeRecord,
    ) -> None: ...

    async def tenant_inputs(self, lease: SummaryLease) -> tuple[SummaryBinding, ...]: ...


class SummaryRepositoryPort(
    SummaryExecutionRepositoryPort,
    SummaryInputRepositoryPort,
    SummaryReaderPort,
    Protocol,
):
    """Compatibility aggregate for composition roots needing the full summary authority."""
