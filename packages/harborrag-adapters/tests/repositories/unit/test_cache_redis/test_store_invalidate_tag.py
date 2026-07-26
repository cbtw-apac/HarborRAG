from __future__ import annotations

import pytest

from .fakes import CONTEXT_V2, FakePipelineV2, FakeRedisClientV2, make_store_v2


@pytest.mark.asyncio
async def test_invalidate_tag_returns_zero_when_no_members() -> None:
    client = FakeRedisClientV2(FakePipelineV2())
    store = make_store_v2(client)

    count = await store.invalidate_tag("tag-a", context=CONTEXT_V2)

    assert count == 0


@pytest.mark.asyncio
async def test_invalidate_tag_deletes_all_tagged_values() -> None:
    pipeline = FakePipelineV2(
        execute_return=[1, 1, 1],
        smembers_return={
            "tenant-a:tag:tag-a": {"tenant-a:value:key-1"},
            "tenant-a:tags:key-1": {"tag-a"},
        },
    )
    client = FakeRedisClientV2(pipeline)
    store = make_store_v2(client)

    count = await store.invalidate_tag("tag-a", context=CONTEXT_V2)

    assert count == 1
    names = [name for name, _ in pipeline.commands]
    # No "srem" against tag-a's own index: that key is unconditionally
    # deleted right after (the last "delete" below), so removing this one
    # member from it first would be wasted work.
    assert names == ["multi", "delete", "delete", "delete"]
    # Both the tag index and every tagged value's own tag-membership key must
    # be watched, so a concurrent re-tag between the read and EXEC aborts the
    # transaction instead of invalidating against stale membership data.
    assert set(pipeline.watched) == {"tenant-a:tag:tag-a", "tenant-a:tags:key-1"}


@pytest.mark.asyncio
async def test_invalidate_tag_srems_only_other_tags_not_the_invalidated_one() -> None:
    """A value tagged with both the invalidated tag and another tag must
    still have its membership in the OTHER tag's index cleaned up."""
    pipeline = FakePipelineV2(
        execute_return=[1, 1, 1, 1],
        smembers_return={
            "tenant-a:tag:tag-a": {"tenant-a:value:key-1"},
            "tenant-a:tags:key-1": {"tag-a", "tag-b"},
        },
    )
    client = FakeRedisClientV2(pipeline)
    store = make_store_v2(client)

    count = await store.invalidate_tag("tag-a", context=CONTEXT_V2)

    assert count == 1
    assert ("srem", ("tenant-a:tag:tag-b", "tenant-a:value:key-1")) in pipeline.commands
    assert (
        "srem",
        ("tenant-a:tag:tag-a", "tenant-a:value:key-1"),
    ) not in pipeline.commands


@pytest.mark.asyncio
async def test_invalidate_tag_batches_membership_reads_for_multiple_values() -> None:
    """Membership reads for every tagged value are pipelined through one
    non-transactional batch pipe (not one sequential SMEMBERS per value)."""
    pipeline = FakePipelineV2(
        execute_return=[1, 1, 1, 1, 1, 1],
        smembers_return={
            "tenant-a:tag:tag-a": {"tenant-a:value:key-1", "tenant-a:value:key-2"},
            "tenant-a:tags:key-1": {"tag-a", "tag-b"},
            "tenant-a:tags:key-2": {"tag-a", "tag-c"},
        },
    )
    client = FakeRedisClientV2(pipeline)
    store = make_store_v2(client)

    count = await store.invalidate_tag("tag-a", context=CONTEXT_V2)

    assert count == 2
    srem_targets = {args for name, args in pipeline.commands if name == "srem"}
    assert srem_targets == {
        ("tenant-a:tag:tag-b", "tenant-a:value:key-1"),
        ("tenant-a:tag:tag-c", "tenant-a:value:key-2"),
    }


@pytest.mark.asyncio
async def test_invalidate_tag_retries_after_concurrent_retag_watch_error() -> None:
    conflicted = FakePipelineV2(
        watch_error=True,
        smembers_return={
            "tenant-a:tag:tag-a": {"tenant-a:value:key-1"},
            "tenant-a:tags:key-1": {"tag-a"},
        },
    )
    succeeded = FakePipelineV2(
        execute_return=[1, 1, 1],
        smembers_return={
            "tenant-a:tag:tag-a": {"tenant-a:value:key-1"},
            "tenant-a:tags:key-1": {"tag-a"},
        },
    )
    client = FakeRedisClientV2([conflicted, succeeded])
    store = make_store_v2(client)

    count = await store.invalidate_tag("tag-a", context=CONTEXT_V2)

    assert count == 1
    assert [name for name, _ in succeeded.commands] == [
        "multi",
        "delete",
        "delete",
        "delete",
    ]
