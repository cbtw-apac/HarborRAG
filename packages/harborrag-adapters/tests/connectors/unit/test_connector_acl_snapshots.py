"""Captured source ACLs map onto document permission snapshots, fail-closed."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from harborrag_adapters.connectors.acl import SourceAcl, SourcePrincipal
from harborrag_adapters.connectors.acl_snapshots import PermissionTarget, permission_snapshot
from harborrag_core.security.context import AccessContext

pytestmark = [pytest.mark.unit, pytest.mark.whitebox]

NOW = datetime(2026, 10, 7, tzinfo=UTC)
TARGET = PermissionTarget(
    tenant_id="acme", document_id="doc-1", resolved_at=NOW, expires_at=NOW + timedelta(hours=12)
)
ADA = SourcePrincipal("user", "acc-ada")
FINANCE = SourcePrincipal("group", "finance")
HR = SourcePrincipal("group", "hr")
DIRECTORY = {
    "user:acc-ada": ["ada@acme"],
    "group:finance": ["ada@acme", "bob@acme"],
    "group:hr": ["bob@acme", "cy@acme"],
}


def reader(principal_id: str) -> AccessContext:
    return AccessContext(principal_id=principal_id, tenant_id="acme")


def test_inherited_document_adds_no_restriction_beyond_its_source():
    snapshot = permission_snapshot(SourceAcl.inherited(), target=TARGET, directory=DIRECTORY)

    assert (snapshot.resource_kind, snapshot.resource_id) == ("document", "doc-1")
    assert snapshot.known and snapshot.public and snapshot.processing_allowed
    assert snapshot.can_read(reader("anyone@acme"), now=NOW)


def test_restricted_levels_intersect_after_directory_expansion():
    acl = SourceAcl.restricted([[ADA, FINANCE], [HR]], reason="confluence_read_restriction")

    snapshot = permission_snapshot(acl, target=TARGET, directory=DIRECTORY)

    assert snapshot.known and not snapshot.public
    assert snapshot.allowed_principal_ids == ("bob@acme",)
    assert snapshot.can_read(reader("bob@acme"), now=NOW)
    assert not snapshot.can_read(reader("ada@acme"), now=NOW)


def test_unmapped_principals_grant_nothing():
    level = SourcePrincipal("jira_security_level", "10100")
    acl = SourceAcl.restricted([[level]], reason="issue_security_level")

    snapshot = permission_snapshot(acl, target=TARGET, directory=DIRECTORY)

    assert snapshot.known and not snapshot.public
    assert snapshot.allowed_principal_ids == ()
    assert not snapshot.can_read(reader("ada@acme"), now=NOW)


def test_unknown_acl_denies_every_reader_and_processing():
    acl = SourceAcl.unknown("item_permissions_unavailable")

    snapshot = permission_snapshot(acl, target=TARGET, directory=DIRECTORY)

    assert not snapshot.known and not snapshot.processing_allowed and not snapshot.public
    assert not snapshot.can_read(reader("ada@acme"), now=NOW)


def test_revision_tracks_effective_principals():
    acl = SourceAcl.restricted([[FINANCE]], reason="r")

    first = permission_snapshot(acl, target=TARGET, directory=DIRECTORY)
    again = permission_snapshot(acl, target=TARGET, directory=DIRECTORY)
    shrunk = permission_snapshot(acl, target=TARGET, directory={"group:finance": ["bob@acme"]})

    assert first.revision == again.revision
    assert first.revision != shrunk.revision


def test_group_keys_follow_the_identifier_the_acl_carries():
    cloud = SourceAcl.restricted(
        [[SourcePrincipal("group", "grp-7f3a", "finance")]], reason="confluence_read_restriction"
    )
    data_center = SourceAcl.restricted(
        [[SourcePrincipal("group", "finance")]], reason="confluence_read_restriction"
    )
    directory = {"group:grp-7f3a": ["ada@acme"], "group:finance": ["bob@acme"]}

    assert permission_snapshot(cloud, target=TARGET, directory=directory).allowed_principal_ids == (
        "ada@acme",
    )
    assert permission_snapshot(
        data_center, target=TARGET, directory=directory
    ).allowed_principal_ids == ("bob@acme",)
    # A directory keyed by display name must not match a Cloud group id.
    by_name = {"group:finance": ["bob@acme"]}
    assert permission_snapshot(cloud, target=TARGET, directory=by_name).allowed_principal_ids == ()
