"""Jira issue security levels captured as source ACL evidence."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from harborrag_adapters.connectors.acl import SourceAcl, SourcePrincipal


def issue_acl(issue: Mapping[str, Any]) -> SourceAcl:
    """Map an issue's security level onto captured ACL evidence.

    ``fields.security`` is null or absent for issues every project browser can
    read, which is exactly the connector's project scope. A security level narrows
    that to the level's members (users, groups, project roles, reporter, assignee,
    ...). Resolving those members needs Jira admin APIs, so the level itself is the
    captured principal and the operator's directory must expand it.
    """

    fields = issue.get("fields")
    security = fields.get("security") if isinstance(fields, Mapping) else None
    if security is None:
        return SourceAcl.inherited()
    level_id = str(security.get("id") or "").strip() if isinstance(security, Mapping) else ""
    if not level_id:
        return SourceAcl.unknown("issue_security_level_unreadable")
    name = str(security.get("name") or "").strip() or None
    return SourceAcl.restricted(
        [[SourcePrincipal("jira_security_level", level_id, name)]],
        reason="issue_security_level",
    )
