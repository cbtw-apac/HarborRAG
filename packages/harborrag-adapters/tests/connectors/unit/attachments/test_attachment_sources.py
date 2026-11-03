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
