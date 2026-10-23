"""Jira issue security levels captured as source ACL evidence.

Does NOT resolve level members; the operator's directory expands the level principal.
"""

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

    The scope is the issue's own project (``jira-project:<KEY>``). A security level
    becomes one ``jira_security_level`` principal with id ``<KEY>/<level id>``.

    Args:
        issue: Issue payload with the ``project`` and ``security`` fields.
        realm: Site realm of the captured principals.

    Returns:
        ``inherited`` ACL when ``security`` is null, ``restricted`` by the level
        principal otherwise, or ``unknown`` when the project or level id is unreadable.
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
