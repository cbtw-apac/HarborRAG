"""Reservations must settle against tokens actually consumed, not the reserved ceiling."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from harborrag_adapters.topology.extractor import ExtractionRun
from harborrag_core.models.chat import HarborChatUsage
from harborrag_core.topology import (
    ChunkExtractionInput,
    ExtractionProfile,
    TopologyJob,
    TopologyPolicy,
)
from harborrag_core.topology.budget import BudgetAdmission, BudgetReservation, UsageSettlement
from harborrag_core.topology.ontology import builtin_ontology
from harborrag_runtime.topology.budgeted_extractor import BudgetedExtractor

pytestmark = pytest.mark.unit


def _profile() -> ExtractionProfile:
    return ExtractionProfile(
        model="primary",
        deployment_revision="deployment-1",
        prompt_digest="prompt-1",
        schema_version="4",
        ontology_version=builtin_ontology().version,
    )


def _job() -> TopologyJob:
    return TopologyJob(
        job_id="job-1",
        tenant_id="tenant-a",
        source_scope_id="scope-a",
        document_id="document-1",
        document_version_id="version-1",
        policy_revision=1,
        policy=TopologyPolicy(
            tenant_id="tenant-a",
            source_scope_id="scope-a",
            enabled=True,
            profile=_profile(),
        ),
        state="running",
        fence=1,
        attempts=1,
    )


class _Delegate:
    async def extract_usage(self, value, *, profile, tenant_id, document_id):
        del value, profile, tenant_id, document_id
        return ExtractionRun(
            output=None,
            usage=HarborChatUsage(prompt_tokens=4800, completion_tokens=2100, total_tokens=6900),
            provider_calls=1,
        )


class _Repository:
    def __init__(self) -> None:
        self.settlements: list[UsageSettlement] = []
        self.reserved: int = 0

    async def reserve_budget(self, job, request):
        del job
        self.reserved = request.input_tokens + request.output_tokens
        return BudgetAdmission(
            admitted=True,
            reservation=BudgetReservation(
                reservation_id=request.reservation_id,
                tenant_id="tenant-a",
                job_id="job-1",
                fence=1,
                reserved_tokens=request.input_tokens + request.output_tokens,
                reserved_cost_usd=Decimal("0.01"),
                expires_at=datetime.now(UTC) + timedelta(seconds=300),
            ),
        )

    async def settle_budget(self, tenant_id, reservation_id, usage):
        del tenant_id, reservation_id
        self.settlements.append(usage)


@pytest.mark.asyncio
async def test_settlement_reports_real_tokens_so_the_ledger_refunds_the_overreservation() -> None:
    repository = _Repository()
    extractor = BudgetedExtractor(
        delegate=_Delegate(),
        repository=repository,
        job=_job(),
        cost_ceiling_usd=Decimal("0.01"),
    )

    await extractor.extract(
        ChunkExtractionInput(chunk_id="chunk-1", content="Alpha"),
        profile=_profile(),
        tenant_id="tenant-a",
        document_id="document-1",
    )

    settled = repository.settlements[0]
    assert settled.input_tokens == 4800
    assert settled.output_tokens == 2100
    # The reservation is a worst-case ceiling; settling actuals is what frees the cap.
    assert repository.reserved > 10 * (4800 + 2100)


class _SilentDelegate:
    """A provider that returns no usage block at all."""

    async def extract_usage(self, value, *, profile, tenant_id, document_id):
        del value, profile, tenant_id, document_id
        return ExtractionRun(output=None, usage=HarborChatUsage(), provider_calls=1)


@pytest.mark.asyncio
async def test_unreported_usage_retains_the_reservation_instead_of_settling_zero() -> None:
    repository = _Repository()
    extractor = BudgetedExtractor(
        delegate=_SilentDelegate(),
        repository=repository,
        job=_job(),
        cost_ceiling_usd=Decimal("0.01"),
    )

    await extractor.extract(
        ChunkExtractionInput(chunk_id="chunk-1", content="Alpha"),
        profile=_profile(),
        tenant_id="tenant-a",
        document_id="document-1",
    )

    # Settling 0/0 would make the call free against the daily cap.
    settled = repository.settlements[0]
    assert settled.input_tokens is None
    assert settled.output_tokens is None
