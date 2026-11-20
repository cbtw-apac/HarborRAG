from __future__ import annotations

import pytest

from harborrag_core.contracts.errors import HarborError
from harborrag_core.domain.connection import Connection
from harborrag_core.domain.document import Document, DocumentRelation
from harborrag_core.domain.element import DocumentElement
from harborrag_core.domain.job import Job
from harborrag_core.domain.member import Member
from harborrag_core.domain.project import Project
from harborrag_core.domain.provenance import DocumentProvenance
from harborrag_core.domain.provider import Provider
from harborrag_core.domain.raw_document import RawDocument
from harborrag_core.domain.retrieval import RetrievalQuery, RetrievalResult
from harborrag_core.security.redaction import redact_mapping, redact_secrets
from harborrag_core.security.url_policy import URLPolicy, URLPolicyError

pytestmark = pytest.mark.unit

PUBLIC_ADDRESS = "93.184.216.34"


def _document(document_id: str) -> Document:
    return Document(
        id=document_id,
        title="Doc",
        content=[],
        content_type="page",
        provenance=DocumentProvenance(source="confluence"),
    )


def _policy_with_public_resolver() -> URLPolicy:
    """Return a policy that resolves every host name to one public address."""
    return URLPolicy(resolver=lambda host, _port: (host if host[0].isdigit() else PUBLIC_ADDRESS,))


def test_url_policy_error_is_a_harbor_error() -> None:
    """URL policy failures can be caught as HarborError."""
    assert issubclass(URLPolicyError, HarborError)
    with pytest.raises(HarborError):
        raise URLPolicyError("boom")


@pytest.mark.parametrize("bad_id", ["", "   ", "docs/line\nbreak.md", "docs/tab\tname.md"])
def test_document_blank_or_control_character_id_is_rejected(bad_id: str) -> None:
    """A document ID that is blank or holds a control character is rejected."""
    with pytest.raises(ValueError, match="id must be non-empty"):
        _document(bad_id)


@pytest.mark.parametrize(
    "document_id",
    [
        "docs/Q3 design review.md",
        "github://acme/handbook/docs/Q3 notes.md",
        "docs/Screenshot 2026-11-20 at 10.00.00\u202fAM.png",
        " leading and trailing .md ",
    ],
)
def test_document_id_from_a_file_name_with_spaces_is_accepted(document_id: str) -> None:
    """File paths become document IDs, so spaces in file and folder names are kept."""
    assert _document(document_id).id == document_id


@pytest.mark.parametrize("bad_id", ["", "   ", "has space"])
def test_job_rejects_blank_or_whitespace_id(bad_id: str) -> None:
    """A job ID that is blank or holds whitespace is rejected."""
    with pytest.raises(ValueError, match="id must be non-empty"):
        Job(
            id=bad_id,
            tenant_id="DEFAULT",
            source_id="src-1",
            project_id="proj-1",
            job_type="bulk_ingest",
        )


@pytest.mark.parametrize("bad_id", ["", "   ", "has space"])
def test_member_rejects_blank_or_whitespace_id(bad_id: str) -> None:
    """A member ID that is blank or holds whitespace is rejected."""
    with pytest.raises(ValueError, match="id must be non-empty"):
        Member(id=bad_id, tenant_id="DEFAULT", subject="user@example.com")


@pytest.mark.parametrize("bad_id", ["", "   ", "has space"])
def test_project_rejects_blank_or_whitespace_id(bad_id: str) -> None:
    """A project ID that is blank or holds whitespace is rejected."""
    with pytest.raises(ValueError, match="id must be non-empty"):
        Project(id=bad_id, tenant_id="DEFAULT", name="Docs", collection="docs_main")


@pytest.mark.parametrize("bad_id", ["", "   ", "has space"])
def test_provider_rejects_blank_or_whitespace_id(bad_id: str) -> None:
    """A provider ID that is blank or holds whitespace is rejected."""
    with pytest.raises(ValueError, match="id must be non-empty"):
        Provider(id=bad_id, tenant_id="DEFAULT", name="OpenAI", family="chat")


@pytest.mark.parametrize("bad_id", ["", "   ", "has space"])
def test_source_config_rejects_blank_or_whitespace_id(bad_id: str) -> None:
    """A connection ID that is blank or holds whitespace is rejected."""
    with pytest.raises(ValueError, match="id must be non-empty"):
        Connection(
            connection_id="docs",
            source_key="p1/docs",
            id=bad_id,
            tenant_id="DEFAULT",
            project_id="proj-1",
            source_type="confluence",
            name="Space",
        )


def test_document_keeps_its_elements_relations_and_defaults() -> None:
    """A document keeps the elements and relations it is built with."""
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


def test_raw_document_text_decodes_bytes_with_the_given_encoding() -> None:
    """Raw text comes back as is, and raw bytes decode with replacement characters."""
    text_raw = RawDocument(id="r1", source="s", content="hello", content_type="text/plain")
    assert text_raw.text() == "hello"
    bytes_raw = RawDocument(
        id="r2", source="s", content="héllo".encode(), content_type="text/plain"
    )
    assert bytes_raw.text() == "héllo"
    assert bytes_raw.text(encoding="ascii") == "h��llo"


def test_redact_secrets_masks_labelled_and_vendor_token_values() -> None:
    """Labelled secrets, bearer tokens and vendor-shaped keys are masked."""
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


def test_redact_secrets_masks_multi_word_unquoted_values() -> None:
    """Every word of an unquoted multi-word secret is masked."""
    redacted = redact_secrets("password: My Secret Passphrase 123")
    assert "My" not in redacted
    assert "Secret" not in redacted
    assert "Passphrase" not in redacted
    assert "123" not in redacted


def test_redact_secrets_does_not_swallow_the_next_field() -> None:
    """Masking stops at the field separator and keeps the next field."""
    redacted = redact_secrets("api_key=abc123&user=alice")
    assert "abc123" not in redacted
    assert "user=alice" in redacted


def test_redact_secrets_masks_suffixed_label_names() -> None:
    """Labels with a suffix after the secret word are still masked."""
    redacted = redact_secrets("db_password_confirm: hunter2")
    assert "hunter2" not in redacted

    redacted = redact_secrets("stripe_secret_key: sk_live_abcdefghij")
    assert "sk_live_abcdefghij" not in redacted


def test_redact_secrets_masks_quoted_multi_word_values() -> None:
    """A quoted secret is masked whole and the next field is kept."""
    redacted = redact_secrets('password="quoted secret value" next_field=ok')
    assert "quoted secret value" not in redacted
    assert "next_field=ok" in redacted


def test_redact_secrets_masks_newer_token_formats() -> None:
    """Fine-grained GitHub tokens and live Stripe keys are masked."""
    redacted = redact_secrets("token=" + "github_pat_" + "a" * 40)
    assert "github_pat_" not in redacted
    redacted = redact_secrets("key=" + "sk_live_" + "a" * 20)
    assert "sk_live_" not in redacted


def test_url_policy_public_https_url_is_accepted() -> None:
    """An HTTPS URL on a public host passes the policy."""
    _policy_with_public_resolver().validate("https://example.com")


@pytest.mark.parametrize("url", ["ftp://example.com", "file:///etc/passwd", "javascript:alert(1)"])
def test_url_policy_non_https_scheme_is_rejected(url: str) -> None:
    """Only HTTPS URLs pass the policy."""
    with pytest.raises(URLPolicyError):
        _policy_with_public_resolver().validate(url)


@pytest.mark.parametrize("host", ["blocked.local", "localhost"])
def test_url_policy_denied_host_is_rejected(host: str) -> None:
    """A host listed in denied_hosts is rejected."""
    with pytest.raises(URLPolicyError):
        URLPolicy(denied_hosts={host}).validate(f"https://{host}/admin")


@pytest.mark.parametrize(
    "url", ["https://169.254.169.254/latest/meta-data/", "https://10.0.0.5/internal"]
)
def test_url_policy_metadata_or_private_address_is_rejected_by_default(url: str) -> None:
    """Cloud-metadata and RFC 1918 addresses are blocked without a denied_hosts entry."""
    with pytest.raises(URLPolicyError):
        _policy_with_public_resolver().validate(url)


@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1/admin",
        "https://localhost/admin",
        "https://LOCALHOST/admin",
        "https://localhost.localdomain/admin",
        "https://localhost./admin",
    ],
)
def test_url_policy_loopback_host_is_rejected_by_default(url: str) -> None:
    """Loopback addresses and localhost names are blocked without a denied_hosts entry.

    Localhost names are matched by name, not by a resolved address. A trailing
    root-zone dot names the same host, so it gets no exemption.
    """
    with pytest.raises(URLPolicyError):
        URLPolicy().validate(url)


def test_redact_mapping_masks_nested_and_cloud_credentials() -> None:
    """Credential fields are masked at any depth and other fields are kept."""
    redacted_config = redact_mapping(
        {"headers": [{"Authorization": "Bearer bearer-secret-123", "Accept": "json"}]}
    )
    assert redacted_config["headers"][0]["Authorization"] == "<redacted>"
    assert redacted_config["headers"][0]["Accept"] == "json"

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

    redacted_config = redact_mapping({"notes": "Authorization: Bearer bearer-secret-123"})
    assert "bearer-secret-123" not in redacted_config["notes"]
    assert redact_mapping({"access_token": "x"})["access_token"] == "<redacted>"
    assert redact_mapping({"max_tokens": 4096})["max_tokens"] == 4096
