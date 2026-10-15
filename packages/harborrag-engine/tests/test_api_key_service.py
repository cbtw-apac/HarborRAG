"""Key verification and issuance rules, exercised with in-memory ports."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from harborrag_core.ports.api_keys import ApiKeyRecord, AuthStoreUnavailable
from harborrag_core.security.api_keys import generate_key, hash_key
from harborrag_engine.security import (
    ApiKeyManagementService,
    ApiKeyPolicyError,
    ApiKeyVerificationService,
)

NOW = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)


class FakeStore:
    """Implements both ports over a dict; records every lookup."""

    def __init__(self) -> None:
        self.records: dict[str, ApiKeyRecord] = {}
        self.lookups: list[str] = []
        self.unavailable = False

    async def get_by_key_id(self, key_id: str) -> ApiKeyRecord | None:
        self.lookups.append(key_id)
        if self.unavailable:
            raise AuthStoreUnavailable("down")
        return self.records.get(key_id)

    async def create(self, record: ApiKeyRecord) -> None:
        self.records[record.key_id] = record

    async def revoke(self, key_id: str, *, at: datetime, by: str, reason: str) -> bool:
        record = self.records.get(key_id)
        if record is None or record.revoked_at is not None:
            return False
        self.records[key_id] = replace(
            record, revoked_at=at, revoked_by=by, revocation_reason=reason
        )
        return True

    async def revoke_by_owner(
        self, tenant_id: str, owner: str, *, at: datetime, by: str, reason: str
    ) -> list[str]:
        revoked = []
        for key_id, record in self.records.items():
            if (record.tenant_id, record.owner) == (tenant_id, owner) and record.revoked_at is None:
                await self.revoke(key_id, at=at, by=by, reason=reason)
                revoked.append(key_id)
        return revoked

    async def list_for_tenant(self, tenant_id: str) -> list[ApiKeyRecord]:
        return [r for r in self.records.values() if r.tenant_id == tenant_id]


def _issued(
    store: FakeStore, environment: str = "prod", **overrides: object
) -> tuple[str, ApiKeyRecord]:
    new = generate_key(environment)
    record = ApiKeyRecord(
        key_id=new.key_id,
        tenant_id="engineering",
        owner="user-huy",
        name="laptop",
        secret_hash=new.secret_hash,
        environment=environment,
        created_at=NOW - timedelta(days=1),
        created_by="operator@host",
        expires_at=NOW + timedelta(days=30),
    )
    record = replace(record, **overrides)  # type: ignore[arg-type]
    store.records[record.key_id] = record
    return new.raw_key, record


@pytest.fixture
def store() -> FakeStore:
    return FakeStore()


@pytest.fixture
def verifier(store: FakeStore) -> ApiKeyVerificationService:
    return ApiKeyVerificationService(store, environment="prod", clock=lambda: NOW)


@pytest.mark.asyncio
async def test_a_valid_key_yields_the_stored_tenant(store, verifier) -> None:
    raw, record = _issued(store)

    verified = await verifier.verify_key(raw)

    assert verified is not None
    assert verified.tenant_id == "engineering"
    assert verified.owner == "user-huy"
    assert verified.key_id == record.key_id
    assert verified.expires_at == record.expires_at


@pytest.mark.asyncio
async def test_wrong_secret_revoked_and_expired_keys_are_denied(store, verifier) -> None:
    raw, _ = _issued(store)
    head, secret = raw.rsplit(".", 1)
    wrong = f"{head}.{('A' if secret[0] != 'A' else 'B') + secret[1:]}"
    assert await verifier.verify_key(wrong) is None

    revoked_raw, _ = _issued(store, revoked_at=NOW - timedelta(minutes=1))
    assert await verifier.verify_key(revoked_raw) is None

    expired_raw, _ = _issued(store, expires_at=NOW - timedelta(seconds=1))
    assert await verifier.verify_key(expired_raw) is None

    # Exactly at expiry is already expired.
    edge_raw, _ = _issued(store, expires_at=NOW)
    assert await verifier.verify_key(edge_raw) is None


@pytest.mark.asyncio
async def test_other_environment_or_malformed_keys_never_reach_the_store(store, verifier) -> None:
    dev_raw, _ = _issued(store, environment="dev")

    assert await verifier.verify_key(dev_raw) is None
    assert await verifier.verify_key("not-a-key") is None
    assert await verifier.verify_key(hash_key(dev_raw)) is None
    assert store.lookups == []


@pytest.mark.asyncio
async def test_a_row_from_another_environment_is_denied_even_if_it_parses(store, verifier) -> None:
    # A prod-formatted key whose row says dev (e.g. a copied database).
    raw, _ = _issued(store, environment="dev")
    raw_prod = raw.replace("hrk_dev_", "hrk_prod_", 1)
    record = store.records[next(iter(store.records))]
    store.records[record.key_id] = replace(record, secret_hash=hash_key(raw_prod))

    assert await verifier.verify_key(raw_prod) is None


@pytest.mark.asyncio
async def test_store_outage_propagates_rather_than_looking_like_a_bad_key(store, verifier) -> None:
    raw, _ = _issued(store)
    store.unavailable = True

    with pytest.raises(AuthStoreUnavailable):
        await verifier.verify_key(raw)


@pytest.fixture
def management(store: FakeStore) -> ApiKeyManagementService:
    return ApiKeyManagementService(store, environment="prod", clock=lambda: NOW)


@pytest.mark.asyncio
async def test_create_stores_only_the_hash_and_returns_the_key_once(store, management) -> None:
    created = await management.create_key(
        operator="operator@host",
        tenant_id="engineering",
        owner="user-huy",
        name="huy-laptop",
        lifetime=timedelta(days=30),
    )

    record = store.records[created.key_id]
    assert record.secret_hash == hash_key(created.raw_key)
    assert created.raw_key not in repr(created)
    assert record.expires_at == NOW + timedelta(days=30)
    assert record.created_by == "operator@host"
    assert record.revoked_at is None
    # The issued key verifies against the same store.
    verifier = ApiKeyVerificationService(store, environment="prod", clock=lambda: NOW)
    assert (await verifier.verify_key(created.raw_key)) is not None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("environment", "lifetime", "message"),
    [
        ("prod", timedelta(days=91), "90 days"),
        ("dev", timedelta(days=8), "7 days"),
        ("prod", timedelta(minutes=59), "at least 1 hour"),
    ],
)
async def test_lifetime_limits(store, environment, lifetime, message) -> None:
    service = ApiKeyManagementService(store, environment=environment, clock=lambda: NOW)

    with pytest.raises(ApiKeyPolicyError, match=message):
        await service.create_key(
            operator="op", tenant_id="t", owner="user-x", name="k", lifetime=lifetime
        )
    assert store.records == {}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("owner", "name", "tenant"),
    [
        ("Huy Nguyen", "k", "t"),
        ("huy", "k", "t"),
        ("user-huy", "Bad Name", "t"),
        ("user-huy", "k", " t"),
        ("user-huy", "k", ""),
    ],
)
async def test_bad_identifiers_are_rejected(store, management, owner, name, tenant) -> None:
    with pytest.raises(ApiKeyPolicyError):
        await management.create_key(
            operator="op", tenant_id=tenant, owner=owner, name=name, lifetime=timedelta(days=1)
        )
    assert store.records == {}


@pytest.mark.asyncio
async def test_revoke_requires_a_reason_and_is_idempotent(store, management) -> None:
    _, record = _issued(store)

    with pytest.raises(ApiKeyPolicyError, match="reason"):
        await management.revoke_key(operator="op", key_id=record.key_id, reason="  ")

    assert await management.revoke_key(operator="op", key_id=record.key_id, reason="lost")
    assert not await management.revoke_key(operator="op", key_id=record.key_id, reason="again")
    assert store.records[record.key_id].revocation_reason == "lost"


@pytest.mark.asyncio
async def test_revoke_owner_only_touches_that_owners_active_keys(store, management) -> None:
    _, a = _issued(store)
    _, b = _issued(store)
    _, already = _issued(store, revoked_at=NOW - timedelta(days=1), revocation_reason="old")
    _, other = _issued(store, owner="user-other")

    revoked = await management.revoke_owner(
        operator="op", tenant_id="engineering", owner="user-huy", reason="left"
    )

    assert set(revoked) == {a.key_id, b.key_id}
    assert store.records[already.key_id].revocation_reason == "old"
    assert store.records[other.key_id].revoked_at is None


@pytest.mark.asyncio
async def test_a_wildcard_tenant_is_refused_at_issue_and_at_verification(
    store, management, verifier
) -> None:
    with pytest.raises(ApiKeyPolicyError, match="wildcard"):
        await management.create_key(
            operator="op", tenant_id="*", owner="user-x", name="k", lifetime=timedelta(days=1)
        )
    assert store.records == {}

    raw, _ = _issued(store, tenant_id="*")  # a row written by other means
    assert await verifier.verify_key(raw) is None
