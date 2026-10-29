"""Connection snapshots: content fingerprints and the secret-free persisted form."""

from __future__ import annotations

import pytest

from harborrag_core.domain.connection_snapshot import ConnectionSnapshot


def _snapshot(**changes: object) -> ConnectionSnapshot:
    values: dict[str, object] = {
        "tenant_id": "acme",
        "source_key": "cpm/tickets",
        "source_type": "jira",
        "credentials": {
            "base_url": "https://acme.atlassian.net",
            "api_token": {"secret_ref": "secret://db/a", "last4": "9f2c", "updated_at": "t1"},
        },
        "config": {"jql": "project = CPM"},
        "tuning": {"requests_per_minute": 600},
    }
    values.update(changes)
    return ConnectionSnapshot(**values)  # type: ignore[arg-type]


def test_fingerprint_ignores_secret_status_and_key_order() -> None:
    restamped = _snapshot(
        credentials={
            "api_token": {"secret_ref": "secret://db/a", "last4": None, "updated_at": "t2"},
            "base_url": "https://acme.atlassian.net",
        }
    )

    assert restamped.fingerprint == _snapshot().fingerprint
    assert _snapshot().fingerprint.startswith("connection-")


@pytest.mark.parametrize(
    "changes",
    [
        {"config": {"jql": "project = HR"}},
        {"tuning": {"requests_per_minute": 60}},
        {"source_key": "cpm/other"},
        {"credentials": {"api_token": {"secret_ref": "secret://db/b"}}},
    ],
)
def test_fingerprint_follows_every_content_change(changes: dict[str, object]) -> None:
    assert _snapshot(**changes).fingerprint != _snapshot().fingerprint


def test_snapshot_round_trips_and_lists_its_references() -> None:
    snapshot = _snapshot()

    assert ConnectionSnapshot.from_json(snapshot.to_json()) == snapshot
    assert snapshot.secret_refs == ["secret://db/a"]


def test_snapshot_refuses_a_raw_secret() -> None:
    with pytest.raises(ValueError, match="secret reference"):
        _snapshot(credentials={"api_token": "ATATT-raw"})
