from __future__ import annotations

from pathlib import Path

import pytest

from harborrag_adapters.repositories.object_store.filesystem.access import (
    decode_key_segment,
    encode_key_segment,
)
from harborrag_adapters.repositories.object_store.filesystem.repository import (
    FilesystemObjectStore,
)
from harborrag_core.schemas.object_store import PutObjectRequest

from .conftest import make_context

# Short on purpose: pytest tmp paths plus the tenant hash approach Windows MAX_PATH.
KEY = "d:%<?/s"


def test_key_segment_encoding_round_trips_windows_unsafe_characters() -> None:
    segment = 'document:a<b>c"d|e?f*g%h\x01'
    encoded = encode_key_segment(segment)
    assert not set('<>:"|?*\x01').intersection(encoded)
    assert decode_key_segment(encoded) == segment
    assert encode_key_segment("plain-name.txt") == "plain-name.txt"


@pytest.mark.asyncio
async def test_encoded_store_round_trips_and_lists_colon_keys(tmp_path: Path) -> None:
    store = FilesystemObjectStore(root=tmp_path)
    store._encode_key_segments = True
    async with store:
        context = make_context()
        await store.put(PutObjectRequest(bucket="bkt", key=KEY, body=b"data"), context=context)

        assert await store.get_bytes("bkt", KEY, byte_range=None, context=context) == b"data"
        listed = await store.list("bkt", "d:", limit=10, context=context)
        assert [item.reference.key for item in listed] == [KEY]
        assert not any(":" in path.name for path in tmp_path.rglob("*"))
        assert await store.delete("bkt", KEY, context=context) is True


@pytest.mark.asyncio
async def test_unencoded_store_keeps_key_segments_verbatim(tmp_path: Path) -> None:
    store = FilesystemObjectStore(root=tmp_path)
    store._encode_key_segments = False
    async with store:
        context = make_context()
        await store.put(PutObjectRequest(bucket="bkt", key="docs/a%41", body=b"1"), context=context)
        assert any(path.name == "a%41" for path in tmp_path.rglob("*"))
        listed = await store.list("bkt", "docs/", limit=10, context=context)
        assert [item.reference.key for item in listed] == ["docs/a%41"]
