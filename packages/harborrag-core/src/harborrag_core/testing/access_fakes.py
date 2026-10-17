"""Dict-backed ``AccessFactsPort`` for tests and in-memory composition."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import datetime

from harborrag_core.base import utc_now
from harborrag_core.contracts.errors import HarborAccessLimitError
from harborrag_core.ports.access import AccessFactsPort
from harborrag_core.security._validation import require_text
from harborrag_core.security.classes import AclClass, ClassId
from harborrag_core.security.context import AccessContext
from harborrag_core.security.decisions import DocumentAccess
from harborrag_core.security.directory import (
    DecisionLogEntry,
    DirectorySnapshot,
    IdentityLink,
    ProjectMember,
)
from harborrag_core.security.entitlements import entitled_scopes
from harborrag_core.topology.permissions import ResolvedPermissionSnapshot


class InMemoryAccessFacts:
    """In-memory ``AccessFactsPort``; every record is keyed by tenant like the SQL stores.

    ``clock`` decides what "now" is when snapshots are tested for readability,
    so tests can pin it. Recorded decisions accumulate in :attr:`decisions`.
    """

    def __init__(self, *, clock: Callable[[], datetime] = utc_now) -> None:
        self._clock = clock
        self._classes: dict[str, dict[ClassId, AclClass]] = {}
        self._documents: dict[str, dict[str, DocumentAccess]] = {}
        self._snapshots: dict[str, dict[ClassId, ResolvedPermissionSnapshot]] = {}
        self._directories: dict[str, dict[str, DirectorySnapshot]] = {}
        self._links: dict[str, tuple[IdentityLink, ...]] = {}
        self._members: dict[str, dict[str, tuple[ProjectMember, ...]]] = {}
        self._rbac_tenants: set[str] = set()
        self._project_scopes: dict[str, dict[str, frozenset[str]]] = {}
        self.decisions: list[DecisionLogEntry] = []

    # AccessClassStore

    async def register_classes(self, classes: Sequence[AclClass]) -> None:
        """Record classes; a different ACL under a registered id raises and records nothing.

        ACLs are compared by their canonical form, so an equivalent ACL (reordered or
        repeated principals and levels, another ``reason``) under a registered id is
        the same class: the registered one is kept.
        """
        staged = {tenant_id: dict(known) for tenant_id, known in self._classes.items()}
        for acl_class in classes:
            tenant_classes = staged.setdefault(acl_class.tenant_id, {})
            existing = tenant_classes.get(acl_class.class_id)
            if existing is None:
                tenant_classes[acl_class.class_id] = acl_class
            elif existing.acl.canonical() != acl_class.acl.canonical():
                raise ValueError(
                    f"ACL class {acl_class.class_id!r} is already registered with a different ACL"
                )
        self._classes = staged

    async def assign_documents(
        self, tenant_id: str, assignments: Mapping[str, DocumentAccess]
    ) -> None:
        """Set the class and datasource of each document id, replacing earlier assignments."""
        require_text(tenant_id, "tenant_id")
        for document_id in assignments:
            require_text(document_id, "document id")
        self._documents.setdefault(tenant_id, {}).update(assignments)

    async def document_access(
        self, tenant_id: str, document_ids: Sequence[str]
    ) -> dict[str, DocumentAccess]:
        """Assignments of the given documents; an unassigned document is absent."""
        assigned = self._documents.get(tenant_id, {})
        return {
            document_id: assigned[document_id]
            for document_id in document_ids
            if document_id in assigned
        }

    async def classes(
        self, tenant_id: str, class_ids: Sequence[ClassId] | None = None
    ) -> dict[ClassId, AclClass]:
        """The tenant's registered classes, all of them or only ``class_ids``."""
        registered = self._classes.get(tenant_id, {})
        if class_ids is None:
            return dict(registered)
        return {class_id: registered[class_id] for class_id in class_ids if class_id in registered}

    async def set_class_snapshots(self, snapshots: Sequence[ResolvedPermissionSnapshot]) -> None:
        """Store each class's snapshot; any non-class snapshot raises and stores nothing."""
        for snapshot in snapshots:
            if snapshot.resource_kind != "class":
                raise ValueError(
                    f"the class store only keeps class snapshots, not {snapshot.resource_kind!r}"
                )
        for snapshot in snapshots:
            self._snapshots.setdefault(snapshot.tenant_id, {})[ClassId(snapshot.resource_id)] = (
                snapshot
            )

    async def class_snapshots(
        self, tenant_id: str, class_ids: Sequence[ClassId]
    ) -> dict[ClassId, ResolvedPermissionSnapshot]:
        """Snapshots of the given classes; a class without one is absent."""
        stored = self._snapshots.get(tenant_id, {})
        return {class_id: stored[class_id] for class_id in class_ids if class_id in stored}

    async def readable_class_ids(self, access: AccessContext, *, limit: int) -> frozenset[ClassId]:
        """Classes whose snapshot lets ``access`` read; above ``limit`` raises, never truncates."""
        now = self._clock()
        readable = frozenset(
            class_id
            for class_id, snapshot in self._snapshots.get(str(access.tenant_id), {}).items()
            if snapshot.can_read(access, now=now)
        )
        if len(readable) > limit:
            raise HarborAccessLimitError(
                f"reader can read {len(readable)} ACL classes, over the limit of {limit}"
            )
        return readable

    # DirectoryStore

    async def replace_directory(self, snapshot: DirectorySnapshot) -> None:
        """Make ``snapshot`` the tenant's directory for its realm."""
        self._directories.setdefault(snapshot.tenant_id, {})[snapshot.realm] = snapshot

    async def directory(self, tenant_id: str, realm: str) -> DirectorySnapshot | None:
        """The tenant's directory for ``realm``, or ``None``."""
        return self._directories.get(tenant_id, {}).get(realm)

    async def links(self, tenant_id: str) -> tuple[IdentityLink, ...]:
        """Every identity link of the tenant, in the order they were stored."""
        return self._links.get(tenant_id, ())

    async def replace_links(self, tenant_id: str, links: Sequence[IdentityLink]) -> None:
        """Make ``links`` the tenant's whole set of identity links."""
        require_text(tenant_id, "tenant_id")
        self._links[tenant_id] = tuple(links)

    async def realm_sync_times(self, tenant_id: str) -> dict[str, datetime]:
        """When each of the tenant's realms was last synced."""
        return {
            realm: snapshot.synced_at
            for realm, snapshot in self._directories.get(tenant_id, {}).items()
        }

    # DecisionLog

    async def record(self, entries: Sequence[DecisionLogEntry]) -> None:
        """Append ``entries`` to :attr:`decisions`."""
        self.decisions.extend(entries)

    # EntitlementStore

    async def replace_project_members(
        self, tenant_id: str, project_id: str, members: Sequence[ProjectMember]
    ) -> None:
        """Make ``members`` the whole membership of the project and enable RBAC for the tenant.

        Enablement is sticky and happens even for an empty list. A member of another
        project raises and changes nothing.
        """
        require_text(tenant_id, "tenant_id")
        require_text(project_id, "project_id")
        for member in members:
            if member.project_id != project_id:
                raise ValueError(
                    f"project member of project {member.project_id!r} given for "
                    f"project {project_id!r}"
                )
        self._rbac_tenants.add(tenant_id)
        tenant_members = self._members.setdefault(tenant_id, {})
        if members:
            tenant_members[project_id] = tuple(members)
        else:
            tenant_members.pop(project_id, None)

    async def project_scopes(self, tenant_id: str, project_id: str) -> frozenset[str]:
        """The datasources of a project; empty when none were set."""
        return self._project_scopes.get(tenant_id, {}).get(project_id, frozenset())

    async def set_project_scopes(
        self, tenant_id: str, project_id: str, source_scope_ids: frozenset[str]
    ) -> None:
        """Make ``source_scope_ids`` the datasources of the project."""
        require_text(tenant_id, "tenant_id")
        require_text(project_id, "project_id")
        for scope_id in source_scope_ids:
            require_text(scope_id, "source scope id")
        self._project_scopes.setdefault(tenant_id, {})[project_id] = frozenset(source_scope_ids)

    async def entitled_source_scopes(
        self, tenant_id: str, principal_id: str
    ) -> frozenset[str] | None:
        """``None`` for a tenant that never configured RBAC; else the principal's union of scopes.

        A membership narrowed to some datasources only ever keeps those that are still
        the project's own, so it can neither reach another project's datasources nor
        keep one its project no longer has.
        """
        if tenant_id not in self._rbac_tenants:
            return None
        memberships = (
            member for members in self._members.get(tenant_id, {}).values() for member in members
        )
        return entitled_scopes(memberships, self._project_scopes.get(tenant_id, {}), principal_id)


def _conforms(facts: InMemoryAccessFacts) -> AccessFactsPort:
    """Fail type checking if the fake stops satisfying the port."""

    return facts


__all__ = ["InMemoryAccessFacts"]
