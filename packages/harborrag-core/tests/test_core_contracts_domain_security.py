from __future__ import annotations

import pytest
from harborrag_core.contracts.capabilities import CapabilityProfile
from harborrag_core.contracts.errors import (
    HarborConfigError,
    HarborConnectionError,
    HarborError,
    HarborImportError,
    HarborNotSupportedError,
)
from harborrag_core.contracts.schemas import FetchResult, Input, InputGet, Status
from harborrag_core.domain.chunk import Chunk
from harborrag_core.domain.data_source import DataSourceType, DocumentMetadata
from harborrag_core.domain.document import DocumentRelation, HarborDocument
from harborrag_core.domain.element import DocumentElement
from harborrag_core.domain.retrieval import RetrievalQuery, RetrievalResult
from harborrag_core.domain.tenant import Tenant
from harborrag_core.observability.metrics import InMemoryMetrics
from harborrag_core.security.redaction import redact_secrets
from harborrag_core.security.url_policy import UrlPolicy


def test_error_hierarchy():
    for error_cls in (
        HarborImportError,
        HarborConfigError,
        HarborConnectionError,
        HarborNotSupportedError,
    ):
        assert issubclass(error_cls, HarborError)
        with pytest.raises(HarborError):
            raise error_cls("boom")


def test_schemas():
    assert Status.SUCCESS == "success"
    assert Status.FAILURE == "failure"
    assert issubclass(InputGet, Input)
    assert FetchResult().message is None


def test_capability_profile():
    profile = CapabilityProfile(sync=True, batch=False)
    profile.require("sync")
    with pytest.raises(ValueError):
        profile.require("missing")
    with pytest.raises(NotImplementedError):
        profile.require("batch")


def test_domain_dataclasses():
    chunk = Chunk(id="c1", document_id="d1", content="hello")
    assert chunk.metadata == {}

    metadata = DocumentMetadata(
        source_system=DataSourceType.CONFLUENCE,
        title="Doc",
        author="alice",
    )
    assert metadata.author == "alice"

    relation = DocumentRelation(
        predicate="parent_of",
        target_id="confluence://SPACE/1",
        target_type="document",
    )
    assert relation.metadata == {}

    element = DocumentElement(id="e1", type="paragraph", content="hello world")
    document = HarborDocument(
        id="doc-1",
        title="Doc",
        source_system=DataSourceType.CONFLUENCE.value,
        content=[element],
        content_type="page",
        metadata=metadata,
        relations=[relation],
    )
    assert document.relations[0].predicate == "parent_of"
    assert document.content[0].content == "hello world"
    assert document.raw is None
    assert element.metadata == {}

    assert RetrievalQuery("q", top_k=2).top_k == 2
    assert RetrievalResult("id", "text", 1.0).score == 1.0

    assert Tenant().id == "default"
    with pytest.raises(ValueError):
        Tenant("")
    with pytest.raises(ValueError):
        Tenant("has space")


def test_security_and_observability_helpers():
    redacted = redact_secrets("api_key=abc token:xyz password=hunter2")
    assert "abc" not in redacted
    assert "xyz" not in redacted
    assert "hunter2" not in redacted

    UrlPolicy().validate("https://example.com")
    with pytest.raises(HarborConnectionError):
        UrlPolicy().validate("ftp://example.com")
    with pytest.raises(HarborConnectionError):
        UrlPolicy(denied_hosts={"blocked.local"}).validate(
            "https://blocked.local/a"
        )

    metrics = InMemoryMetrics()
    metrics.increment("items", provider="mock")
    metrics.observe("latency", 1.25)
    assert metrics.counters["items{provider=mock}"] == 1
    assert metrics.observations["latency"] == [1.25]
