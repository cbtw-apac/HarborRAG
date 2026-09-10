"""Admission control keys on the user, the credential and the tenant.

One shared service credential must not put every human behind it in a single
bucket, and a tenant holding many credentials must still meet an aggregate
ceiling. Reserving is all-or-nothing: a rejection at any tier leaves the
tiers that had room untouched.
"""

from __future__ import annotations

import copy

import pytest
from test_api_capacity import FakeRedis, limits, scope

from harborrag_app.api.capacity import LocalApiCapacityLimiter, RedisApiCapacityLimiter
from harborrag_app.api.capacity_scope import plan_capacity_buckets
from harborrag_core.contracts.errors import HarborRateLimitError

_ALICE = scope(user="alice", principal="shared-credential")
_BOB = scope(user="bob", principal="shared-credential")


@pytest.mark.asyncio
async def test_one_user_cannot_starve_the_others_behind_a_shared_credential() -> None:
    limiter = LocalApiCapacityLimiter(limits(user=(1, 5), principal=(50, 50), tenant=(50, 50)))

    await limiter.reserve(_ALICE)
    with pytest.raises(HarborRateLimitError, match="rate") as rejection:
        await limiter.reserve(_ALICE)

    assert rejection.value.details["limit_scope"] == "user"
    # The tenant and the credential still have room, so Bob is unaffected.
    assert await limiter.reserve(_BOB)


@pytest.mark.asyncio
async def test_tenant_aggregate_rejects_while_every_user_is_under_their_own_limit() -> None:
    limiter = LocalApiCapacityLimiter(limits(user=(2, 5), principal=(50, 50), tenant=(3, 50)))
    alice = scope(user="alice", principal="alice-key")
    bob = scope(user="bob", principal="bob-key")

    await limiter.reserve(alice)
    await limiter.reserve(alice)
    await limiter.reserve(bob)

    with pytest.raises(HarborRateLimitError, match="rate") as rejection:
        await limiter.reserve(bob)

    assert rejection.value.details["limit_scope"] == "tenant"
    assert rejection.value.details["limit_kind"] == "requests_per_minute"


@pytest.mark.asyncio
async def test_rejected_reservation_leaves_the_surviving_buckets_unchanged() -> None:
    limiter = LocalApiCapacityLimiter(limits(user=(5, 5), principal=(5, 5), tenant=(1, 5)))
    await limiter.reserve(scope(user="alice", principal="alice-key"))
    windows = copy.deepcopy(limiter._windows)
    leases = copy.deepcopy(limiter._leases)

    with pytest.raises(HarborRateLimitError):
        await limiter.reserve(scope(user="bob", principal="bob-key"))

    assert limiter._windows == windows
    assert limiter._leases == leases


@pytest.mark.asyncio
async def test_rejected_reservation_does_not_leak_a_concurrency_lease() -> None:
    limiter = LocalApiCapacityLimiter(limits(user=(50, 1), principal=(50, 50), tenant=(50, 1)))
    alice = scope(user="alice", principal="alice-key")
    bob = scope(user="bob", principal="bob-key")
    alice_lease = await limiter.reserve(alice)

    with pytest.raises(HarborRateLimitError, match="concurrent") as rejection:
        await limiter.reserve(bob)
    await limiter.release(alice, alice_lease)

    assert rejection.value.details["limit_scope"] == "tenant"
    # Bob's own single-lease bucket would be full had the rejection leaked.
    assert await limiter.reserve(bob)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tier_limits", "tier"),
    [
        ({"user": (50, 1)}, "user"),
        ({"principal": (50, 1)}, "principal"),
        ({"tenant": (50, 1)}, "tenant"),
    ],
)
async def test_concurrency_rejection_names_the_tier_that_bound(
    tier_limits: dict[str, tuple[int, int]],
    tier: str,
) -> None:
    limiter = LocalApiCapacityLimiter(
        limits(**{"user": (50, 50), "principal": (50, 50), "tenant": (50, 50), **tier_limits})
    )
    await limiter.reserve(_ALICE)

    with pytest.raises(HarborRateLimitError, match="concurrent") as rejection:
        await limiter.reserve(_ALICE)

    assert rejection.value.details["limit_scope"] == tier
    assert rejection.value.details["limit_kind"] == "max_inflight"
    assert rejection.value.details["retry_after_seconds"] == 1
    assert str(rejection.value).endswith(f"the {tier} scope")


def test_an_unset_user_id_claim_collapses_the_two_identical_keys() -> None:
    """``Principal.user_id`` defaults to the subject when no claim is set."""

    buckets = plan_capacity_buckets(
        scope(user="same", principal="same"),
        limits(user=(2, 1), principal=(3, 2)),
    )

    assert len(buckets) == 2
    assert buckets[0].requests_per_minute == 2
    assert buckets[0].rate_tier == "user"
    assert buckets[0].max_inflight == 1
    assert buckets[0].inflight_tier == "user"
    assert buckets[1].rate_tier == "tenant"


def test_the_collapsed_bucket_reports_whichever_tier_is_stricter() -> None:
    buckets = plan_capacity_buckets(
        scope(user="same", principal="same"),
        limits(user=(9, 9), principal=(3, 2)),
    )

    assert buckets[0].rate_tier == "principal"
    assert buckets[0].requests_per_minute == 3
    assert buckets[0].inflight_tier == "principal"


@pytest.mark.asyncio
async def test_an_unset_user_id_claim_does_not_halve_the_effective_rate() -> None:
    limiter = LocalApiCapacityLimiter(limits(user=(2, 5), principal=(2, 5), tenant=(50, 50)))
    caller = scope(user="same", principal="same")

    assert await limiter.reserve(caller)
    assert await limiter.reserve(caller)
    with pytest.raises(HarborRateLimitError, match="rate") as rejection:
        await limiter.reserve(caller)

    assert rejection.value.details["limit_scope"] == "user"


def test_distinct_user_and_principal_ids_key_three_independent_buckets() -> None:
    buckets = plan_capacity_buckets(_ALICE, limits())

    assert [bucket.rate_tier for bucket in buckets] == ["user", "principal", "tenant"]
    assert len({bucket.identity for bucket in buckets}) == 3


@pytest.mark.asyncio
async def test_tenant_overrides_apply_and_unknown_tenants_keep_the_defaults() -> None:
    limiter = LocalApiCapacityLimiter(
        limits(user=(1, 5), principal=(50, 50), tenant=(50, 50)),
        300.0,
        {"enterprise": limits(user=(3, 5), principal=(50, 50), tenant=(50, 50))},
    )
    enterprise = scope(user="alice", principal="alice-key", tenant="enterprise")
    starter = scope(user="alice", principal="alice-key", tenant="starter")

    await limiter.reserve(enterprise)
    await limiter.reserve(enterprise)
    await limiter.reserve(starter)

    with pytest.raises(HarborRateLimitError, match="rate") as rejection:
        await limiter.reserve(starter)
    assert rejection.value.details["limit_scope"] == "user"
    # The override is per tenant: the enterprise bucket still has its third slot.
    assert await limiter.reserve(enterprise)


@pytest.mark.asyncio
async def test_redis_reservations_send_the_overridden_tenant_limits() -> None:
    client = FakeRedis(1)
    limiter = RedisApiCapacityLimiter(
        client,
        limits(user=(10, 2), principal=(20, 3), tenant=(30, 4)),
        30,
        {"enterprise": limits(user=(11, 5), principal=(22, 6), tenant=(33, 7))},
    )

    await limiter.reserve(scope(tenant="enterprise"))
    await limiter.reserve(scope(tenant="starter"))

    assert list(client.calls[0][8:15]) == [3, 11, 5, 22, 6, 33, 7]
    assert list(client.calls[1][8:15]) == [3, 10, 2, 20, 3, 30, 4]
