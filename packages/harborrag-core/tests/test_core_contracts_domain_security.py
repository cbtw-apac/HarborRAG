from __future__ import annotations

import pytest

from harborrag_core.contracts.errors import HarborError
from harborrag_core.domain.element import DocumentElement
from harborrag_core.domain.job import Job
from harborrag_core.domain.member import Member
from harborrag_core.domain.normalized_document import Document, DocumentRelation
from harborrag_core.domain.project import Project
from harborrag_core.domain.provenance import DocumentProvenance
from harborrag_core.domain.provider import Provider
from harborrag_core.domain.raw_document import RawDocument
from harborrag_core.domain.retrieval import RetrievalQuery, RetrievalResult
from harborrag_core.domain.source_config import SourceConfig
from harborrag_core.security.redaction import redact_mapping, redact_secrets
from harborrag_core.security.url_policy import URLPolicy, URLPolicyError


def test_error_hierarchy():
    assert issubclass(URLPolicyError, HarborError)
    with pytest.raises(HarborError):
        raise URLPolicyError("boom")


@pytest.mark.parametrize("bad_id", ["", "   ", "has space"])
def test_document_rejects_blank_or_whitespace_id(bad_id: str) -> None:
    with pytest.raises(ValueError, match="id must be non-empty"):
        Document(
            id=bad_id,
            title="Doc",
            content=[],
            content_type="page",
            provenance=DocumentProvenance(source="confluence"),
        )


@pytest.mark.parametrize("bad_id", ["", "   ", "has space"])
def test_job_rejects_blank_or_whitespace_id(bad_id: str) -> None:
    with pytest.raises(ValueError, match="id must be non-empty"):
        Job(id=bad_id, source_id="src-1", project_id="proj-1", job_type="bulk_ingest")


@pytest.mark.parametrize("bad_id", ["", "   ", "has space"])
def test_member_rejects_blank_or_whitespace_id(bad_id: str) -> None:
    with pytest.raises(ValueError, match="id must be non-empty"):
        Member(id=bad_id, subject="user@example.com")


@pytest.mark.parametrize("bad_id", ["", "   ", "has space"])
def test_project_rejects_blank_or_whitespace_id(bad_id: str) -> None:
    with pytest.raises(ValueError, match="id must be non-empty"):
        Project(id=bad_id, name="Docs", collection="docs_main")


@pytest.mark.parametrize("bad_id", ["", "   ", "has space"])
def test_provider_rejects_blank_or_whitespace_id(bad_id: str) -> None:
    with pytest.raises(ValueError, match="id must be non-empty"):
        Provider(id=bad_id, name="OpenAI", family="chat")


@pytest.mark.parametrize("bad_id", ["", "   ", "has space"])
def test_source_config_rejects_blank_or_whitespace_id(bad_id: str) -> None:
    with pytest.raises(ValueError, match="id must be non-empty"):
        SourceConfig(id=bad_id, project_id="proj-1", source_type="confluence", name="Space")


def test_domain_dataclasses():
    provenance = DocumentProvenance(
        source="confluence",
        author="alice",
    )
    assert provenance.author == "alice"

    relation = DocumentRelation(
        predicate="parent_of",
        target_id="confluence://SPACE/1",
        target_type="document",
    )
    assert relation.metadata == {}

    element = DocumentElement(id="e1", type="paragraph", content="hello world")
    document = Document(
        id="doc-1",
        title="Doc",
        content=[element],
        content_type="page",
        provenance=provenance,
        relations=[relation],
    )
    assert document.relations[0].predicate == "parent_of"
    assert document.content[0].content == "hello world"
    assert document.raw is None
    assert element.metadata == {}

    assert RetrievalQuery("q", top_k=2).top_k == 2
    assert RetrievalResult("id", "text", 1.0).score == 1.0

    text_raw = RawDocument(id="r1", source="s", content="hello", content_type="text/plain")
    assert text_raw.text() == "hello"
    bytes_raw = RawDocument(
        id="r2", source="s", content="héllo".encode(), content_type="text/plain"
    )
    assert bytes_raw.text() == "héllo"
    assert bytes_raw.text(encoding="ascii") == "h��llo"


def test_security_helpers():
    redacted = redact_secrets("api_key=abc token:xyz password=hunter2")
    assert "abc" not in redacted
    assert "xyz" not in redacted
    assert "hunter2" not in redacted

    redacted = redact_secrets("secret=shh credential:cred123")
    assert "shh" not in redacted
    assert "cred123" not in redacted

    redacted = redact_secrets("Authorization: Bearer abc.def.ghi")
    assert "abc.def.ghi" not in redacted

    redacted = redact_secrets(
        "aws=AKIAABCDEFGHIJKLMNOP github=ghp_"
        + "a" * 36
        + " openai=sk-"
        + "a" * 20
        + " google=AIza"
        + "a" * 25
        + " slack=xoxb-1234567890"
    )
    assert "AKIAABCDEFGHIJKLMNOP" not in redacted
    assert "ghp_" + "a" * 36 not in redacted
    assert "sk-" + "a" * 20 not in redacted
    assert "AIza" + "a" * 25 not in redacted
    assert "xoxb-1234567890" not in redacted

    # Nested Authorization header inside a list of mappings must be masked,
    # not copied through unchanged.
    redacted_config = redact_mapping(
        {"headers": [{"Authorization": "Bearer bearer-secret-123", "Accept": "json"}]}
    )
    assert redacted_config["headers"][0]["Authorization"] == "<redacted>"
    assert redacted_config["headers"][0]["Accept"] == "json"

    # Common cloud access-key field names are masked even without the
    # word "secret" or "token" in the key.
    redacted_config = redact_mapping(
        {
            "aws_access_key_id": "AKIAABCDEFGHIJKLMNOP",
            "access_key": "abc123",
            "private_key": "-----BEGIN PRIVATE KEY-----",
        }
    )
    assert redacted_config["aws_access_key_id"] == "<redacted>"
    assert redacted_config["access_key"] == "<redacted>"
    assert redacted_config["private_key"] == "<redacted>"

    # A secret embedded in free text under a non-sensitive key is still
    # caught by the string-value pass-through to redact_secrets().
    redacted_config = redact_mapping({"notes": "Authorization: Bearer bearer-secret-123"})
    assert "bearer-secret-123" not in redacted_config["notes"]

    # Regression test: keys containing the substring 'token' but not the
    # credential word (e.g. 'max_tokens') must not be redacted.
    redacted_config = redact_mapping({"max_tokens": 4096})
    assert redacted_config["max_tokens"] == 4096

    URLPolicy().validate("https://example.com")
    with pytest.raises(URLPolicyError):
        URLPolicy().validate("ftp://example.com")
    with pytest.raises(URLPolicyError):
        URLPolicy().validate("file:///etc/passwd")
    with pytest.raises(URLPolicyError):
        URLPolicy().validate("javascript:alert(1)")
    with pytest.raises(URLPolicyError):
        URLPolicy(denied_hosts={"blocked.local"}).validate("https://blocked.local/a")
    # Cloud-metadata and RFC1918 addresses are blocked by default, without
    # needing to be added to denied_hosts.
    with pytest.raises(URLPolicyError):
        URLPolicy().validate("https://169.254.169.254/latest/meta-data/")
    with pytest.raises(URLPolicyError):
        URLPolicy().validate("https://10.0.0.5/internal")
    with pytest.raises(URLPolicyError):
        URLPolicy().validate("https://127.0.0.1/admin")
    with pytest.raises(URLPolicyError):
        URLPolicy(denied_hosts={"localhost"}).validate("https://localhost/admin")
    # "localhost" and its common aliases resolve to loopback and are blocked
    # by default too, without needing to be added to denied_hosts -- unlike
    # 127.0.0.1 this is a symbolic name, not a literal IP the ipaddress check
    # below would catch.
    with pytest.raises(URLPolicyError):
        URLPolicy().validate("https://localhost/admin")
    with pytest.raises(URLPolicyError):
        URLPolicy().validate("https://LOCALHOST/admin")
    with pytest.raises(URLPolicyError):
        URLPolicy().validate("https://localhost.localdomain/admin")
    # A trailing root-zone dot is DNS/HTTP-client equivalent to the bare
    # name and must not bypass the check on that technicality.
    with pytest.raises(URLPolicyError):
        URLPolicy().validate("https://localhost./admin")
