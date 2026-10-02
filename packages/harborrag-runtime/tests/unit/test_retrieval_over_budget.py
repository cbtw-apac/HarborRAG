"""Above the ACL enumeration budget, retrieval answers only from readable documents."""

from __future__ import annotations

import pytest
from topology_retrieval_support import CanonicalTopology, TopologyVectors, record, service

from harborrag_core.contracts.errors import HarborLimitExceededError
from harborrag_core.indexing import VectorSearchResult
from harborrag_core.security import AccessContext

READER = AccessContext(principal_id="reader", tenant_id="tenant-1", corpus_mode="source_acl")


class _OverBudgetTopology(CanonicalTopology):
    """Too many readable documents to enumerate; document-2 is not readable."""

    async def allowed_document_ids(self, tenant_id, *, access, limit=10000):
        del tenant_id, access
        raise HarborLimitExceededError(
            "authorized document enumeration exceeds the configured budget",
            details={"limit": limit},
        )

    async def authorized_document_ids(self, tenant_id, document_ids, *, access):
        del tenant_id, access
        return set(document_ids) & {"document-1"}


class _ReadableAndUnreadableVectors(TopologyVectors):
    """A readable hit, a readable stale hit, and an active and a stale unreadable hit."""

    def _results(self, collection):
        if "evidence" not in collection:
            return []
        hits = [
            *self.records,
            record("chunk-3", "document-2", "version-old"),
            record("chunk-4", "document-1", "version-old"),
        ]
        return [
            VectorSearchResult(id=item.id, score=0.9, raw_score=0.9, payload=item.payload)
            for item in hits
        ]


@pytest.mark.asyncio
async def test_over_budget_reader_sees_only_readable_results_and_counts() -> None:
    retrieval = service(_OverBudgetTopology(), vectors=_ReadableAndUnreadableVectors())

    report = await retrieval.retrieve("Alpha", tenant_id="tenant-1", top_k=5, access=READER)

    assert [result.text for result in report.results] == ["Alpha depends on Beta."]
    # Diagnostics must not reveal how many matches exist in unreadable documents.
    assert report.diagnostics.candidate_hits == 1
    # The search stage saw unreadable documents, so its rejection counts are
    # withheld; the readable stale hit is under-reported by design.
    assert report.diagnostics.stale_candidates == 0
    assert report.diagnostics.unpublished_candidates == 0
    assert report.diagnostics.malformed_candidates == 0
