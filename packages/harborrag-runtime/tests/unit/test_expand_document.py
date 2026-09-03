from __future__ import annotations

import pytest
from retrieval_test_support import (
    FakeCanonicalDocuments,
    FakeDocumentSnapshots,
)
from retrieval_test_support import (
    policy as _policy,
)
from retrieval_test_support import (
    resources as _resources,
)

from harborrag_core.contracts.errors import HarborCapabilityError, HarborNotFoundError
from harborrag_runtime.retrieval import RuntimeRetrievalService


@pytest.mark.asyncio
async def test_expand_document_returns_the_canonical_document_for_the_owning_tenant() -> None:
    canonical_documents = FakeCanonicalDocuments()
    service = RuntimeRetrievalService(
        resources=_resources(
            document_snapshots=FakeDocumentSnapshots(),
            canonical_documents=canonical_documents,
        ),
        policy=_policy(),
    )

    expansion = await service.expand_document("document-1", tenant_id="tenant-1")

    assert expansion.document.id == "document-1"
    assert expansion.document.title == "Release guide"
    assert expansion.document_version_id == "version-1"
    assert len(canonical_documents.requests) == 1


@pytest.mark.asyncio
async def test_expand_document_hides_documents_owned_by_another_tenant() -> None:
    service = RuntimeRetrievalService(
        resources=_resources(
            document_snapshots=FakeDocumentSnapshots(),
            canonical_documents=FakeCanonicalDocuments(),
        ),
        policy=_policy(),
    )

    with pytest.raises(HarborNotFoundError):
        await service.expand_document("document-1", tenant_id="tenant-2")


@pytest.mark.asyncio
async def test_expand_document_raises_when_not_configured() -> None:
    service = RuntimeRetrievalService(resources=_resources(), policy=_policy())

    with pytest.raises(HarborCapabilityError):
        await service.expand_document("document-1", tenant_id="tenant-1")


@pytest.mark.asyncio
async def test_expand_document_rejects_blank_document_id() -> None:
    service = RuntimeRetrievalService(
        resources=_resources(
            document_snapshots=FakeDocumentSnapshots(),
            canonical_documents=FakeCanonicalDocuments(),
        ),
        policy=_policy(),
    )

    with pytest.raises(ValueError, match="document_id"):
        await service.expand_document(" ", tenant_id="tenant-1")
