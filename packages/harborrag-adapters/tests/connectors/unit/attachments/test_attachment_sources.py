from __future__ import annotations

import pytest

from harborrag_adapters.connectors.attachments import (
    AttachmentSourceGateway,
    AttachmentSourcePolicy,
)
from harborrag_adapters.connectors.exceptions import FetchError
from harborrag_adapters.connectors.file_filters import ExtensionFilter


def _gateway(downloads: dict[str, bytes]) -> AttachmentSourceGateway:
    def download(url: str, *, max_bytes: int | None) -> bytes | None:
        content = downloads.get(url)
        if content is not None and max_bytes is not None and len(content) > max_bytes:
            raise FetchError("downloaded attachment exceeds cap")
        return content

    return AttachmentSourceGateway(
        download_fn=download,
        policy=AttachmentSourcePolicy(
            base_url="https://source.example",
            max_size_bytes=32,
        ),
    )


def test_descriptor_admission_does_not_download_attachment_bytes() -> None:
    downloads = {"https://source.example/files/1": b"content"}
    gateway = _gateway(downloads)

    descriptor = gateway.describe(
        [
            {
                "id": "1",
                "filename": "runbook.md",
                "mimeType": "text/markdown",
                "size": 7,
                "content": "https://source.example/files/1",
                "updated": "2026-07-30T00:00:00Z",
            }
        ]
    )[0]

    assert descriptor.status == "admitted"
    assert descriptor.size_bytes == 7
    assert gateway.fetch(descriptor) == b"content"


def test_descriptor_rejects_untrusted_download_origin() -> None:
    descriptor = _gateway({}).describe(
        [
            {
                "id": "1",
                "filename": "runbook.md",
                "mimeType": "text/markdown",
                "size": 7,
                "content": "https://attacker.example/files/1",
            }
        ]
    )[0]

    assert descriptor.status == "failed"
    assert descriptor.download_url == ""
    with pytest.raises(FetchError, match="not admitted"):
        _gateway({}).fetch(descriptor)


def test_descriptor_skips_an_attachment_the_extension_filter_drops() -> None:
    gateway = AttachmentSourceGateway(
        download_fn=lambda url, *, max_bytes: b"",
        policy=AttachmentSourcePolicy(
            base_url="https://source.example",
            extensions=ExtensionFilter.of(allowed=["pdf", "md"], excluded=["md"]),
        ),
    )
    attachment = {"mimeType": "text/markdown", "size": 7, "content": "https://source.example/f"}

    kept, dropped = gateway.describe(
        [
            {**attachment, "id": "1", "filename": "report.pdf"},
            {**attachment, "id": "2", "filename": "runbook.md"},
        ]
    )

    assert kept.status == "admitted"
    assert (dropped.status, dropped.reason) == (
        "skipped",
        "extension .md is in excluded_extensions",
    )


def _attachment(**overrides: object) -> dict[str, object]:
    return {
        "id": "1",
        "filename": "runbook.md",
        "mimeType": "text/markdown",
        "size": 7,
        "content": "https://source.example/files/1",
        **overrides,
    }


def _policy_gateway(**policy: object) -> AttachmentSourceGateway:
    return AttachmentSourceGateway(
        download_fn=lambda url, *, max_bytes: b"",
        policy=AttachmentSourcePolicy(base_url="https://source.example", **policy),
    )


def test_descriptor_skips_an_attachment_over_the_size_limit() -> None:
    [descriptor] = _gateway({}).describe([_attachment(size=33)])

    assert descriptor.status == "skipped"
    assert descriptor.reason == "size 33 exceeds max_attachment_size_bytes 32"


def test_fetch_rejects_a_body_larger_than_the_limit() -> None:
    gateway = AttachmentSourceGateway(
        download_fn=lambda url, *, max_bytes: b"x" * 40,
        policy=AttachmentSourcePolicy(base_url="https://source.example", max_size_bytes=32),
    )
    [descriptor] = gateway.describe([_attachment()])

    with pytest.raises(FetchError, match="exceeds max_attachment_size_bytes"):
        gateway.fetch(descriptor)


def test_descriptor_callback_can_skip() -> None:
    gateway = _policy_gateway(process_callback=lambda media, size, title: (False, "policy denied"))

    [descriptor] = gateway.describe([_attachment()])

    assert (descriptor.status, descriptor.reason) == ("skipped", "policy denied")


def test_descriptor_failures_stay_with_their_attachment_and_are_redacted() -> None:
    def callback(media_type: str, size_bytes: int, title: str) -> tuple[bool, str]:
        if title == "broken.md":
            raise RuntimeError("token=abc123secret leaked")
        return True, ""

    failed, malformed, admitted = _policy_gateway(process_callback=callback).describe(
        [
            _attachment(id="1", filename="broken.md"),
            _attachment(id="2", size="not-a-number"),
            _attachment(id="3"),
        ]
    )

    assert failed.status == "failed"
    assert "abc123secret" not in (failed.reason or "")
    assert malformed.status == "failed"
    assert admitted.status == "admitted"


def test_fail_on_error_reraises_a_redacted_error() -> None:
    def callback(media_type: str, size_bytes: int, title: str) -> tuple[bool, str]:
        raise RuntimeError("token=SECRET123 leaked")

    gateway = _policy_gateway(process_callback=callback, fail_on_error=True)

    with pytest.raises(FetchError) as caught:
        gateway.describe([_attachment()])

    assert "SECRET123" not in str(caught.value)


def test_descriptor_marks_an_unknown_file_type_unsupported() -> None:
    [descriptor] = _policy_gateway().describe(
        [_attachment(filename="archive.zzz", mimeType="application/x-unknown")]
    )

    assert descriptor.status == "unsupported"
