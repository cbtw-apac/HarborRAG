"""Whose limits one API request must satisfy, and how those buckets key.

Admission control keys on three tiers instead of the credential alone: the
end user, the API principal (the credential itself), and the tenant
aggregate. Without the user tier one shared service credential fronting many
humans gives all of them a single bucket, so one user starves the rest;
without the tenant tier a customer holding many credentials has no ceiling
at all and can consume the whole deployment.

This module is a leaf: it imports nothing from ``harborrag_app.api`` so both
the limiters and ``ApiSettings`` can depend on it.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from harborrag_core.contracts.errors import HarborRateLimitError

CapacityTier = Literal["user", "principal", "tenant"]
#: Bucket shared by principals whose token is not scoped to exactly one
#: tenant (wildcard dev principals, multi-tenant service credentials). Adding
#: a second tenant id to a token must not mint a private aggregate ceiling,
#: so those callers land in one shared pool instead of escaping the limit.
SHARED_TENANT_KEY = "*"


@dataclass(frozen=True, slots=True)
class CapacityScope:
    """The three identities one request is charged against."""

    tenant_id: str
    principal_id: str
    user_id: str


@dataclass(frozen=True, slots=True)
class CapacityTierLimits:
    """Fixed-window request rate and concurrent-request ceiling for one tier."""

    requests_per_minute: int
    max_inflight: int

    def __post_init__(self) -> None:
        _validate_positive_int(self.requests_per_minute, "requests_per_minute")
        _validate_positive_int(self.max_inflight, "max_inflight")


@dataclass(frozen=True, slots=True)
class CapacityLimits:
    """One deployment's (or one tenant's) limits for all three tiers."""

    user: CapacityTierLimits
    principal: CapacityTierLimits
    tenant: CapacityTierLimits

    @classmethod
    def uniform(cls, requests_per_minute: int, max_inflight: int) -> CapacityLimits:
        """Apply the same numbers to every tier (development and tests)."""
        tier = CapacityTierLimits(requests_per_minute, max_inflight)
        return cls(user=tier, principal=tier, tenant=tier)


@dataclass(frozen=True, slots=True)
class CapacityBucket:
    """One keyed counter pair plus the tier each dimension's limit came from.

    ``rate_tier``/``inflight_tier`` are what the rejection reports, so an
    operator learns whether the user, the credential, or the tenant ceiling
    bound -- they can differ on a merged bucket (see ``plan_capacity_buckets``).
    """

    identity: str
    requests_per_minute: int
    rate_tier: CapacityTier
    max_inflight: int
    inflight_tier: CapacityTier


def resolve_capacity_limits(
    tenant_id: str,
    default_limits: CapacityLimits,
    tenant_overrides: Mapping[str, CapacityLimits],
) -> CapacityLimits:
    """Pick a tenant's configured limits, falling back to the global default."""
    return tenant_overrides.get(tenant_id, default_limits)


def plan_capacity_buckets(
    scope: CapacityScope,
    limits: CapacityLimits,
) -> tuple[CapacityBucket, ...]:
    """List the buckets a reservation must satisfy, most specific first.

    Tier identities are namespaced before hashing, so a user id equal to a
    principal id still keys two independent counters. When they *are* the same
    identity -- ``HARBORRAG_AUTH_USER_ID_CLAIM`` unset, so ``Principal.user_id``
    defaults to the subject -- the two tiers collapse into one bucket carrying
    the stricter limit of each dimension. Incrementing a single key twice for
    one request would otherwise halve the effective limit.
    """
    tenant = _tier_bucket("tenant", scope, scope.tenant_id, limits.tenant)
    if scope.user_id == scope.principal_id:
        return (_strictest_bucket(scope, limits.user, limits.principal), tenant)
    return (
        _tier_bucket("user", scope, scope.user_id, limits.user),
        _tier_bucket("principal", scope, scope.principal_id, limits.principal),
        tenant,
    )


def capacity_hash_tag(scope: CapacityScope) -> str:
    """Identity every bucket of one request shares as its Redis Cluster tag.

    Keying the tag on the tenant lands all six keys in one slot, which is what
    lets a single Lua script check and commit every tier atomically.
    """
    return hashlib.sha256(f"tenant-slot:{scope.tenant_id}".encode()).hexdigest()


def _tier_bucket(
    tier: CapacityTier,
    scope: CapacityScope,
    subject: str,
    limits: CapacityTierLimits,
) -> CapacityBucket:
    return CapacityBucket(
        identity=_tier_identity(tier, scope.tenant_id, subject),
        requests_per_minute=limits.requests_per_minute,
        rate_tier=tier,
        max_inflight=limits.max_inflight,
        inflight_tier=tier,
    )


def _strictest_bucket(
    scope: CapacityScope,
    user: CapacityTierLimits,
    principal: CapacityTierLimits,
) -> CapacityBucket:
    rate_tier: CapacityTier = (
        "user" if user.requests_per_minute <= principal.requests_per_minute else "principal"
    )
    inflight_tier: CapacityTier = (
        "user" if user.max_inflight <= principal.max_inflight else "principal"
    )
    return CapacityBucket(
        identity=_tier_identity("principal", scope.tenant_id, scope.principal_id),
        requests_per_minute=min(user.requests_per_minute, principal.requests_per_minute),
        rate_tier=rate_tier,
        max_inflight=min(user.max_inflight, principal.max_inflight),
        inflight_tier=inflight_tier,
    )


def rate_limit_rejection(tier: CapacityTier) -> HarborRateLimitError:
    """Reject a request-rate reservation, naming the tier that bound.

    The scope travels in the error envelope's ``details`` so an operator can
    tell "this user is hot" from "this tenant is at its aggregate ceiling"
    without any other tenant's identity or configured numbers leaking.
    """
    error = HarborRateLimitError(f"API request rate limit exceeded for the {tier} scope")
    error.details["limit_scope"] = tier
    error.details["limit_kind"] = "requests_per_minute"
    return error


def inflight_limit_rejection(tier: CapacityTier) -> HarborRateLimitError:
    """Reject a concurrent-request reservation, naming the tier that bound."""
    error = HarborRateLimitError(
        f"API concurrent request limit exceeded for the {tier} scope",
        retry_after_seconds=1,
    )
    error.details["limit_scope"] = tier
    error.details["limit_kind"] = "max_inflight"
    return error


def _tier_identity(tier: CapacityTier, tenant_id: str, subject: str) -> str:
    """Hash a bucket key, scoped to the tenant and namespaced by tier.

    Every tier is tenant-scoped so the local and Redis layouts agree: the
    Redis keys already sit under a tenant hash tag, and a user id reused by
    two tenants must not share one bucket across them. Namespacing by tier
    keeps a user id equal to a principal id on two independent counters.
    """
    return hashlib.sha256(f"{tier}:{tenant_id}:{subject}".encode()).hexdigest()


def _validate_positive_int(value: int, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"API {name} must be a positive integer")


__all__ = [
    "SHARED_TENANT_KEY",
    "CapacityBucket",
    "CapacityLimits",
    "CapacityScope",
    "CapacityTier",
    "CapacityTierLimits",
    "capacity_hash_tag",
    "inflight_limit_rejection",
    "plan_capacity_buckets",
    "rate_limit_rejection",
    "resolve_capacity_limits",
]
