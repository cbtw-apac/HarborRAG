"""Control-plane domain aggregates: project, activity, member, provider, settings."""

from datetime import UTC
from typing import get_args

import pytest
from harborrag_core.domain.activity import ActivityEntry
from harborrag_core.domain.member import Member, Role
from harborrag_core.domain.project import Project, ProjectStats
from harborrag_core.domain.provider import Provider
from harborrag_core.domain.settings import WorkspaceSettings


@pytest.mark.whitebox
def test_role_literals_match_rbac_plan() -> None:
    """Role covers exactly four RBAC roles; Principal must reuse
    this literal instead of redefining it."""
    assert set(get_args(Role)) == {"owner", "admin", "editor", "reader"}


@pytest.mark.whitebox
def test_project_defaults() -> None:
    """A fresh Project is active with zeroed stats and UTC timestamps."""
    project = Project(id="p1", name="Docs", collection="docs_main")
    assert project.status == "active"
    assert project.description == ""
    assert project.stats == ProjectStats()
    assert project.created_at.tzinfo is UTC
    assert project.updated_at.tzinfo is UTC


@pytest.mark.whitebox
def test_project_stats_defaults() -> None:
    """ProjectStats starts at zero with no last sync."""
    stats = ProjectStats()
    assert (stats.documents, stats.chunks, stats.size_bytes) == (0, 0, 0)
    assert stats.last_sync_at is None


@pytest.mark.whitebox
def test_activity_entry_shape() -> None:
    """ActivityEntry carries the audit fields from the plan activity table."""
    entry = ActivityEntry(
        id="a1",
        actor="nguyen.vu@cbtw.tech",
        verb="created",
        entity_type="source",
        entity_id="s1",
        summary="Created source docs",
    )
    assert entry.created_at.tzinfo is UTC


@pytest.mark.whitebox
def test_member_and_provider_and_settings_defaults() -> None:
    """Member defaults to reader (least privilege); Provider carries a
    secret_ref only (never a key value); WorkspaceSettings wraps a dict."""
    member = Member(id="m1", subject="user@cbtw.tech")
    assert member.role == "reader"
    provider = Provider(id="pr1", name="OpenAI", family="chat")
    assert provider.secret_ref is None
    assert provider.config == {}
    assert WorkspaceSettings().data == {}
