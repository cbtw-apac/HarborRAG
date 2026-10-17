"""Jira issue security levels captured as source ACL evidence."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from harborrag_adapters.connectors.acl import SourceAcl, SourcePrincipal

PROJECT_SCOPE_PREFIX = "jira-project:"


def _project_key(fields: Mapping[str, Any]) -> str | None:
    project = fields.get("project")
    key = project.get("key") if isinstance(project, Mapping) else None
    if not isinstance(key, str) or not key.strip():
        return None
    return key.strip()


def issue_acl(issue: Mapping[str, Any], *, realm: str) -> SourceAcl:
    """Map an issue's security level onto captured ACL evidence.

    The ACL is relative to the issue's own project (``jira-project:<KEY>``), read
    from ``fields.project.key``. An issue whose project cannot be read has no scope
    to inherit from and is ``unknown``, which is withheld.

    ``fields.security`` is null or absent for issues every project browser can
    read, which is exactly that scope. A security level narrows it to the level's
    members (users, groups, project roles, reporter, assignee, ...). Resolving
    those members needs Jira admin APIs, so the level itself is the captured
    principal, in the site's ``realm``, and the operator's directory must expand it.

    The level principal's id is ``<project key>/<level id>``, with the same project
    key as the scope. One security scheme can serve several projects, and a level's
    project-role and project-lead members resolve in the issue's project, so the
    same level in two projects is two principals.
    """

    fields = issue.get("fields")
    if not isinstance(fields, Mapping):
        return SourceAcl.unknown(reason="issue_project_unreadable", realm=realm)
    project_key = _project_key(fields)
    if project_key is None:
        return SourceAcl.unknown(reason="issue_project_unreadable", realm=realm)
    scope = f"{PROJECT_SCOPE_PREFIX}{project_key}"
    security = fields.get("security")
    if security is None:
        return SourceAcl.inherited(realm=realm, scope=scope)
    level_id = str(security.get("id") or "").strip() if isinstance(security, Mapping) else ""
    if not level_id:
        return SourceAcl.unknown(reason="issue_security_level_unreadable", realm=realm, scope=scope)
    name = str(security.get("name") or "").strip() or None
    level = SourcePrincipal(
        realm=realm,
        kind="jira_security_level",
        id=f"{project_key}/{level_id}",
        display_name=name,
    )
    return SourceAcl.restricted(
        realm=realm, scope=scope, levels=[[level]], reason="issue_security_level"
    )
