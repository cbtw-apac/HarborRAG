"""Admission checks and streaming guards for source files.

Every check happens before or while bytes move, never after: a file whose
declared size or type is not allowed is skipped without a download, and a
stream that turns out larger than declared stops at the limit.
"""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass
from pathlib import PurePosixPath

from harborrag_core.sources import DocKey, ItemFailure, SkipReason

from .errors import ResponseTooLarge


def _extension(filename: str) -> str:
    return PurePosixPath(filename).suffix.lower()


@dataclass(frozen=True, slots=True)
class BlobPolicy:
    """Which files may be read: extension allowlist, size cap, optional MIME allowlist."""

    extensions: frozenset[str]
    max_bytes: int
    media_types: frozenset[str] | None = None

    @classmethod
    def of(
        cls,
        extensions: Iterable[str],
        max_megabytes: float,
        media_types: Iterable[str] | None = None,
    ) -> BlobPolicy:
        normalized = frozenset(
            ext.lower() if ext.startswith(".") else f".{ext.lower()}" for ext in extensions
        )
        return cls(
            normalized,
            int(max_megabytes * 1024 * 1024),
            frozenset(media_types) if media_types is not None else None,
        )

    def admit(
        self,
        key: DocKey,
        filename: str,
        *,
        size: int | None = None,
        media_type: str | None = None,
    ) -> ItemFailure | None:
        """``None`` when the file may be read, else a skip naming why not."""

        if _extension(filename) not in self.extensions:
            return ItemFailure(
                SkipReason.UNSUPPORTED_TYPE,
                detail=f"{filename}: extension not allowed",
                key=key,
                skipped=True,
            )
        if self.media_types is not None and media_type is not None:
            base = media_type.split(";", 1)[0].strip().lower()
            if base not in self.media_types:
                return ItemFailure(
                    SkipReason.UNSUPPORTED_TYPE,
                    detail=f"{filename}: {base} not allowed",
                    key=key,
                    skipped=True,
                )
        if size is not None and size > self.max_bytes:
            return ItemFailure(
                SkipReason.TOO_LARGE,
                detail=f"{filename}: {size} bytes exceeds {self.max_bytes}",
                key=key,
                skipped=True,
            )
        return None


async def capped_chunks(chunks: AsyncIterator[bytes], max_bytes: int) -> AsyncIterator[bytes]:
    """Pass chunks through, raising :class:`ResponseTooLarge` past ``max_bytes``."""

    total = 0
    async for chunk in chunks:
        total += len(chunk)
        if total > max_bytes:
            raise ResponseTooLarge(max_bytes)
        yield chunk


class HashingTee:
    """Computes SHA-256 and size of a stream while passing it on."""

    def __init__(self) -> None:
        self._hash = hashlib.sha256()
        self.size = 0

    async def tee(self, chunks: AsyncIterator[bytes]) -> AsyncIterator[bytes]:
        async for chunk in chunks:
            self._hash.update(chunk)
            self.size += len(chunk)
            yield chunk

    @property
    def hexdigest(self) -> str:
        return self._hash.hexdigest()
