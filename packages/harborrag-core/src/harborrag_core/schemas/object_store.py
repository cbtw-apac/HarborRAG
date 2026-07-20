from __future__ import annotations

from collections.abc import AsyncIterable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import BinaryIO

from pydantic import Field

from harborrag_core.base import StrictModel, utc_now

ObjectBody = bytes | bytearray | memoryview | Path | BinaryIO | AsyncIterable[bytes]


@dataclass(frozen=True, slots=True)
class PutObjectRequest:
    """Describes a tenant-safe object upload request."""

    bucket: str
    key: str
    body: ObjectBody
    content_type: str | None = None
    metadata: dict[str, str] = field(default_factory=dict)
    checksum_sha256: str | None = None
    if_none_match: bool = False


class ObjectReference(StrictModel):
    """Represents object reference data shared across HarborRAG layers."""

    bucket: str
    key: str
    uri: str
    version_id: str | None = None
    etag: str | None = None
    checksum_sha256: str | None = None
    size_bytes: int = Field(ge=0)
    content_type: str | None = None
    created_at: datetime = Field(default_factory=utc_now)


class ObjectMetadata(StrictModel):
    """Represents object metadata data shared across HarborRAG layers."""

    reference: ObjectReference
    metadata: dict[str, str] = Field(default_factory=dict)
    last_modified: datetime | None = None


class ObjectStoreCapabilities(StrictModel):
    """Describes supported object store features."""

    multipart_upload: bool = False
    object_versioning: bool = False
    presigned_urls: bool = False
    conditional_writes: bool = False
    range_downloads: bool = True
    server_side_encryption: bool = False
    streaming_upload: bool = True
    streaming_download: bool = True
