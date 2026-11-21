from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from harborrag_core.ports.api_keys import ApiKeyRecord, AuthStoreUnavailable
from harborrag_core.security.api_keys import NewKey, generate_key
from harborrag_engine.security import ApiKeyVerificationService

START = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)


class _Clock:
    """Wall clock that moves only when a test advances it."""

    def __init__(self) -> None:
        self.now = START

    def __call__(self) -> datetime:
        return self.now


class _CountingReader:
    """ApiKeyReader over a dict that counts every lookup."""

    def __init__(self) -> None:
        self.records: dict[str, ApiKeyRecord] = {}
        self.lookups = 0
        self.unavailable = False

    async def get_by_key_id(self, key_id: str) -> ApiKeyRecord | None:
        self.lookups += 1
        if self.unavailable:
            raise AuthStoreUnavailable("down")
        return self.records.get(key_id)

    def issue(self) -> str:
        new = generate_key("prod")
        self.store(new)
        return new.raw_key

    def store(self, new: NewKey) -> None:
        self.records[new.key_id] = ApiKeyRecord(
            key_id=new.key_id,
            tenant_id="engineering",
            owner="user-demo",
            name="laptop",
            secret_hash=new.secret_hash,
            environment="prod",
            created_at=START - timedelta(days=1),
            created_by="operator@host",
            expires_at=START + timedelta(days=30),
        )


@pytest.fixture
def clock() -> _Clock:
    return _Clock()


@pytest.fixture
def reader() -> _CountingReader:
    return _CountingReader()


@pytest.fixture
def verifier(reader: _CountingReader, clock: _Clock) -> ApiKeyVerificationService:
    return ApiKeyVerificationService(reader, environment="prod", clock=clock)


def _wrong_secret(raw_key: str) -> str:
    head, secret = raw_key.rsplit(".", 1)
    return f"{head}.{('A' if secret[0] != 'A' else 'B') + secret[1:]}"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_repeated_unknown_key_costs_one_lookup_while_its_miss_is_remembered(
    reader: _CountingReader, verifier: ApiKeyVerificationService
) -> None:
    bogus = generate_key("prod").raw_key

    for _ in range(20):
        assert await verifier.verify_key(bogus) is None

    assert reader.lookups == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_key_stored_after_its_id_missed_verifies_once_the_miss_expires(
    reader: _CountingReader, verifier: ApiKeyVerificationService, clock: _Clock
) -> None:
    late = generate_key("prod")
    assert await verifier.verify_key(late.raw_key) is None
    reader.store(late)
    clock.now += timedelta(seconds=5)

    verified = await verifier.verify_key(late.raw_key)

    assert verified is not None
    assert verified.key_id == late.key_id


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_wrong_secret_for_a_real_key_id_never_locks_out_its_holder(
    reader: _CountingReader, verifier: ApiKeyVerificationService
) -> None:
    raw_key = reader.issue()

    for _ in range(5):
        assert await verifier.verify_key(_wrong_secret(raw_key)) is None

    assert await verifier.verify_key(raw_key) is not None
    assert reader.lookups == 6


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_store_outage_is_not_remembered_as_an_unknown_key(
    reader: _CountingReader, verifier: ApiKeyVerificationService
) -> None:
    raw_key = reader.issue()
    reader.unavailable = True
    with pytest.raises(AuthStoreUnavailable):
        await verifier.verify_key(raw_key)
    reader.unavailable = False

    assert await verifier.verify_key(raw_key) is not None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_distinct_unknown_key_ids_each_still_cost_one_lookup(
    reader: _CountingReader, verifier: ApiKeyVerificationService
) -> None:
    for _ in range(20):
        assert await verifier.verify_key(generate_key("prod").raw_key) is None

    assert reader.lookups == 20


@pytest.mark.unit
@pytest.mark.asyncio
async def test_remembered_misses_stay_bounded_by_dropping_the_oldest(
    reader: _CountingReader, verifier: ApiKeyVerificationService
) -> None:
    from harborrag_engine.security import api_key_service

    first = generate_key("prod").raw_key
    await verifier.verify_key(first)
    for _ in range(api_key_service._MISS_CACHE_SIZE):
        await verifier.verify_key(generate_key("prod").raw_key)
    lookups = reader.lookups

    await verifier.verify_key(first)

    assert reader.lookups == lookups + 1
