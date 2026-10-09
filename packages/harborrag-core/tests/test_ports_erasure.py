"""A user-erasure journal record holds identifiers and counts, never content."""

from __future__ import annotations

import pytest

from harborrag_core.ports.erasure import UserErasureRecord


def test_a_new_record_is_running_with_an_opaque_id() -> None:
    record = UserErasureRecord(tenant_id="ACME", subject_user_id="user-1", actor="admin-1")

    assert record.status == "running"
    assert record.erasure_id.startswith("erasure-")
    assert record.counts == {}
    assert record.finished_at is None


@pytest.mark.parametrize("field", ["tenant_id", "subject_user_id", "actor"])
def test_identifiers_must_not_be_blank(field: str) -> None:
    values = {"tenant_id": "ACME", "subject_user_id": "user-1", "actor": "admin-1", field: " "}
    with pytest.raises(ValueError, match=field):
        UserErasureRecord(**values)


def test_counts_must_not_be_negative() -> None:
    with pytest.raises(ValueError, match="negative"):
        UserErasureRecord(
            tenant_id="ACME", subject_user_id="user-1", actor="admin-1", counts={"sessions": -1}
        )
