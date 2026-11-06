"""Connection aggregate, builder, slugs and keyset paging."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from harborrag_core.contracts.errors import (
    HarborConflictError,
    HarborPreconditionFailedError,
    HarborValidationError,
)
from harborrag_core.domain.connection import (
    Connection,
    ConnectionBuilder,
    slugify_connection_id,
    unique_connection_id,
)
from harborrag_core.domain.connection_paging import decode_connection_cursor
from harborrag_core.testing.control_plane_fakes import FakeConnectionRepository

_NOW = datetime(2026, 10, 29, 9, 0, tzinfo=UTC)


def _jira(connection_id: str = "cpm-tickets", *, project_id: str = "cpm") -> Connection:
    return (
        ConnectionBuilder.for_new(
            tenant_id="acme", project_id=project_id, connection_id=connection_id
        )
        .source_type("jira")
        .credentials(
            {"base_url": "https://acme.atlassian.net", "api_token": {"secret_ref": "secret://db/1"}}
        )
        .config({"jql": "project = CPM"})
        .actor("u_huy")
        .build(now=_NOW)
    )


def test_builder_applies_defaults_for_a_new_connection() -> None:
    connection = _jira()

    assert connection.name == "cpm-tickets"
    assert connection.source_key == "cpm/cpm-tickets"
    assert connection.ref == "cpm/cpm-tickets"
    assert connection.version == 1
    assert (connection.created_by, connection.updated_by) == ("u_huy", "u_huy")
    assert connection.created_at == connection.updated_at == _NOW
    assert connection.id.startswith("src_")


def test_builder_from_existing_keeps_identity_and_creation_stamps() -> None:
    original = _jira()
    later = datetime(2026, 10, 30, tzinfo=UTC)

    updated = (
        ConnectionBuilder.from_existing(original)
        .name("CPM tickets")
        .config({"jql": "project = HR"})
        .actor("u_ops")
        .build(now=later)
    )

    assert (updated.id, updated.source_key) == (original.id, original.source_key)
    assert (updated.created_by, updated.created_at) == ("u_huy", _NOW)
    assert (updated.updated_by, updated.updated_at) == ("u_ops", later)
    assert updated.config == {"jql": "project = HR"}


def test_builder_pins_an_imported_source_key_but_never_changes_a_stored_one() -> None:
    imported = (
        ConnectionBuilder.for_new(tenant_id="acme", project_id="default", connection_id="jira-main")
        .source_type("jira")
        .source_key("jira-main")
        .build()
    )
    assert imported.source_key == "jira-main"

    with pytest.raises(ValueError, match="immutable"):
        ConnectionBuilder.from_existing(imported).source_key("default/jira-main")


@pytest.mark.parametrize("bad", ["", "Upper", "-lead", "has space", "a" * 64, "dot.ted"])
def test_connection_id_must_match_the_spec_pattern(bad: str) -> None:
    with pytest.raises(ValueError, match="connection_id must match"):
        ConnectionBuilder.for_new(
            tenant_id="acme", project_id="cpm", connection_id=bad
        ).source_type("jira").build()


def test_credentials_reject_raw_secret_values() -> None:
    with pytest.raises(ValueError, match="secret reference"):
        ConnectionBuilder.for_new(
            tenant_id="acme", project_id="cpm", connection_id="x"
        ).source_type("jira").credentials({"api_token": "ATATT-raw"}).build()


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("CPM Tickets", "cpm-tickets"),
        ("  __Docs & Wiki!!  ", "docs-wiki"),
        ("***", "connection"),
        ("x" * 80, "x" * 63),
    ],
)
def test_slugify_connection_id(name: str, expected: str) -> None:
    assert slugify_connection_id(name) == expected


def test_unique_connection_id_suffixes_within_the_length_limit() -> None:
    assert unique_connection_id("docs", set()) == "docs"
    assert unique_connection_id("docs", {"docs", "docs-2"}) == "docs-3"
    assert len(unique_connection_id("x" * 63, {"x" * 63})) == 63


@pytest.mark.asyncio
async def test_fake_put_creates_then_overwrites_and_advances_version() -> None:
    repository = FakeConnectionRepository()

    created, outcome = await repository.put(_jira())
    assert (outcome, created.version) == ("created", 1)

    changed = ConnectionBuilder.from_existing(created).config({"jql": "x"}).build()
    stored, outcome = await repository.put(changed, expected_version=1)
    assert (outcome, stored.version) == ("overwritten", 2)

    with pytest.raises(HarborPreconditionFailedError):
        await repository.put(changed, expected_version=1)
    with pytest.raises(HarborPreconditionFailedError):
        await repository.put(changed, create_only=True)
    with pytest.raises(HarborPreconditionFailedError):
        await repository.put(_jira("new-one"), expected_version=3)


@pytest.mark.asyncio
async def test_fake_put_rejects_a_source_key_already_used_in_the_tenant() -> None:
    repository = FakeConnectionRepository()
    await repository.put(_jira())
    clash = (
        ConnectionBuilder.for_new(tenant_id="acme", project_id="other", connection_id="b")
        .source_type("jira")
        .source_key("cpm/cpm-tickets")
        .build()
    )

    with pytest.raises(HarborConflictError):
        await repository.put(clash)


@pytest.mark.asyncio
async def test_fake_list_pages_attention_first_with_an_opaque_cursor() -> None:
    repository = FakeConnectionRepository()
    for project_id, connection_id in [("b", "one"), ("a", "two"), ("a", "one")]:
        await repository.put(_jira(connection_id, project_id=project_id))
    await repository.set_status(tenant_id="acme", source_key="b/one", status="needs_reauth")

    first, cursor = await repository.list(tenant_ids=None, limit=2)
    assert [c.ref for c in first] == ["b/one", "a/one"]
    assert cursor is not None
    second, last = await repository.list(tenant_ids=None, limit=2, cursor=cursor)
    assert [c.ref for c in second] == ["a/two"]
    assert last is None


def test_connection_cursor_rejects_garbage() -> None:
    with pytest.raises(HarborValidationError):
        decode_connection_cursor("not-a-cursor")
