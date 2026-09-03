from __future__ import annotations

from types import SimpleNamespace

import pytest

from harborrag_core.domain.document import Document, DocumentRelation
from harborrag_core.domain.element import DocumentElement
from harborrag_core.domain.provenance import DocumentProvenance
from harborrag_core.schemas.ids import TenantId
from harborrag_core.security import AccessContext
from harborrag_runtime.contracts import ExpandDocumentRequest
from harborrag_runtime.retrieval import RuntimeDocumentExpansion
from harborrag_runtime.sdk.facades import RetrievalFacade


class FakeRetrievalService:
    def __init__(self, expansion: RuntimeDocumentExpansion) -> None:
        self.expansion = expansion
        self.calls: list[tuple[str, str]] = []

    async def expand_document(self, document_id: str, *, tenant_id: str, access=None):
        del access
        self.calls.append((document_id, tenant_id))
        return self.expansion


def _owner(service: FakeRetrievalService) -> SimpleNamespace:
    async def _service():
        return service

    return SimpleNamespace(_retrieval_service=_service)


def _document() -> Document:
    return Document(
        id="document-1",
        title="Release guide",
        content=[
            DocumentElement(
                id="el-1",
                type="paragraph",
                content="The activity timeout is 30 seconds.",
            ),
            DocumentElement(
                id="el-2",
                type="paragraph",
                content="LGTM, ship it.",
                metadata={"role": "comment", "comment_id": "c1", "author": "alice"},
            ),
        ],
        content_type="page",
        provenance=DocumentProvenance(source="confluence"),
        relations=[
            DocumentRelation(
                predicate="has_attachment",
                target_id="confluence://SPACE/attachment-1",
                target_type="document",
            )
        ],
    )


def _request() -> ExpandDocumentRequest:
    return ExpandDocumentRequest(
        access=AccessContext(principal_id="subject-1", tenant_id=TenantId("tenant-1")),
        document_id="document-1",
    )


@pytest.mark.asyncio
async def test_expand_document_keeps_inline_comments_in_text() -> None:
    expansion = RuntimeDocumentExpansion(document=_document(), document_version_id="version-1")
    facade = RetrievalFacade(_owner(FakeRetrievalService(expansion)))

    response = await facade.expand_document(_request())

    assert response.text == "The activity timeout is 30 seconds.\n\nLGTM, ship it."


@pytest.mark.asyncio
async def test_expand_document_surfaces_relations_such_as_attachments() -> None:
    expansion = RuntimeDocumentExpansion(document=_document(), document_version_id="version-1")
    facade = RetrievalFacade(_owner(FakeRetrievalService(expansion)))

    response = await facade.expand_document(_request())

    assert len(response.relations) == 1
    relation = response.relations[0]
    assert relation.predicate == "has_attachment"
    assert relation.target_id == "confluence://SPACE/attachment-1"
    assert relation.target_type == "document"


@pytest.mark.asyncio
async def test_expand_document_forwards_document_id_and_tenant_to_the_service() -> None:
    expansion = RuntimeDocumentExpansion(document=_document(), document_version_id="version-1")
    service = FakeRetrievalService(expansion)
    facade = RetrievalFacade(_owner(service))

    await facade.expand_document(_request())

    assert service.calls == [("document-1", "tenant-1")]
