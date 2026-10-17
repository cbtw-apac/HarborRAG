"""Ports behind access decisions: ACL classes, directories, entitlements and the log.

The stores are split by what they hold so an adapter can back each with its own
table, and :class:`AccessFactsPort` is their union for callers that need all of
them. :class:`AccessService` is the one door the engine uses to turn a verified
credential into an :class:`~harborrag_core.security.context.AccessContext` and to
decide resources. The last three ports are what a connector offers the directory
sync: the directory itself, a change feed and a permission oracle.

Every method is ``async`` and fails closed: anything that cannot be decided
raises, it is never answered with a partial or permissive result.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Protocol

from harborrag_core.ports.identity import VerifiedToken
from harborrag_core.security.classes import AclClass, ClassId
from harborrag_core.security.context import AccessContext
from harborrag_core.security.decisions import Decision, DocumentAccess
from harborrag_core.security.directory import (
    ChangeBatch,
    DecisionLogEntry,
    DirectorySnapshot,
    IdentityLink,
    ProjectMember,
)
from harborrag_core.security.resources import ResourceRef
from harborrag_core.sources.acl import SourcePrincipal
from harborrag_core.topology.permissions import ResolvedPermissionSnapshot


class AccessClassStore(Protocol):
    """ACL classes, the class each document belongs to, and each class's snapshot."""

    async def register_classes(self, classes: Sequence[AclClass]) -> None:
        """Record classes; registering one again is a no-op, a different ACL under its id raises."""
        ...

    async def assign_documents(
        self, tenant_id: str, assignments: Mapping[str, DocumentAccess]
    ) -> None:
        """Set the class and datasource of each document id, replacing any earlier assignment."""
        ...

    async def document_access(
        self, tenant_id: str, document_ids: Sequence[str]
    ) -> dict[str, DocumentAccess]:
        """Assignments of the given documents; an unassigned document is absent from the result."""
        ...

    async def classes(
        self, tenant_id: str, class_ids: Sequence[ClassId] | None = None
    ) -> dict[ClassId, AclClass]:
        """The tenant's registered classes, all of them or only ``class_ids``."""
        ...

    async def set_class_snapshots(self, snapshots: Sequence[ResolvedPermissionSnapshot]) -> None:
        """Store the current readability snapshot of each class, replacing earlier ones."""
        ...

    async def class_snapshots(
        self, tenant_id: str, class_ids: Sequence[ClassId]
    ) -> dict[ClassId, ResolvedPermissionSnapshot]:
        """Snapshots of the given classes; a class without one is absent from the result."""
        ...

    async def readable_class_ids(self, access: AccessContext, *, limit: int) -> frozenset[ClassId]:
        """Classes ``access`` may read, by the rule of ``ResolvedPermissionSnapshot.can_read``.

        Raises :class:`~harborrag_core.contracts.errors.HarborAccessLimitError`
        when more than ``limit`` classes are readable; the result is never truncated.
        """
        ...


class DirectoryStore(Protocol):
    """Synced realm directories and the links from HarborRAG principals to accounts."""

    async def replace_directory(self, snapshot: DirectorySnapshot) -> None:
        """Make ``snapshot`` the tenant's directory for its realm."""
        ...

    async def directory(self, tenant_id: str, realm: str) -> DirectorySnapshot | None:
        """The tenant's directory for ``realm``, or ``None`` when it was never synced."""
        ...

    async def links(self, tenant_id: str) -> tuple[IdentityLink, ...]:
        """Every identity link of the tenant."""
        ...

    async def replace_links(self, tenant_id: str, links: Sequence[IdentityLink]) -> None:
        """Make ``links`` the tenant's whole set of identity links."""
        ...

    async def realm_sync_times(self, tenant_id: str) -> dict[str, datetime]:
        """When each of the tenant's realms was last synced."""
        ...


class DecisionLog(Protocol):
    """Append-only record of access decisions."""

    async def record(self, entries: Sequence[DecisionLogEntry]) -> None:
        """Append ``entries`` in order."""
        ...


class EntitlementStore(Protocol):
    """AUTA RBAC: which datasources a principal is entitled to through its projects."""

    async def replace_project_members(
        self, tenant_id: str, project_id: str, members: Sequence[ProjectMember]
    ) -> None:
        """Make ``members`` the whole membership of the project."""
        ...

    async def project_scopes(self, tenant_id: str, project_id: str) -> frozenset[str]:
        """The datasources (source scope ids) of a project; empty when it has none."""
        ...

    async def set_project_scopes(
        self, tenant_id: str, project_id: str, source_scope_ids: frozenset[str]
    ) -> None:
        """Make ``source_scope_ids`` the datasources of the project."""
        ...

    async def entitled_source_scopes(
        self, tenant_id: str, principal_id: str
    ) -> frozenset[str] | None:
        """Datasources the principal is entitled to read.

        ``None`` when the tenant has no project members at all, meaning RBAC is
        not configured and the entitlement gate is skipped. Otherwise the union,
        over the principal's memberships, of the membership's ``source_scope_ids``
        or, when that is ``None``, of the project's scopes. A principal with no
        membership gets an empty set, which entitles nothing.
        """
        ...


class AccessFactsPort(AccessClassStore, DirectoryStore, EntitlementStore, DecisionLog, Protocol):
    """Everything the access service and the sync jobs read and write."""


class AccessService(Protocol):
    """Turn credentials into access contexts and decide resources for them."""

    async def authenticate(self, token: VerifiedToken, *, tenant_id: str | None) -> AccessContext:
        """Build the access context a verified token has in a tenant; raise when it has none."""
        ...

    async def classes_for(self, principal_id: str, tenant_id: str) -> frozenset[ClassId]:
        """The classes a principal belongs to; raises rather than truncating."""
        ...

    async def check_batch(
        self, access: AccessContext, refs: Sequence[ResourceRef]
    ) -> list[Decision]:
        """One decision per ref, in order; a ref that cannot be decided is denied."""
        ...


class DirectoryReaderPort(Protocol):
    """A connector's view of its source's identity directory."""

    async def read(self, tenant_id: str) -> DirectorySnapshot:
        """Read the current directory of the source for the tenant."""
        ...


class ChangeFeedPort(Protocol):
    """A connector's feed of what changed in its source."""

    async def changed_since(self, since: datetime) -> ChangeBatch:
        """Items changed since ``since`` and whether the directory changed."""
        ...


class PermissionOraclePort(Protocol):
    """A connector's authority on whether an account can read items right now."""

    async def can_read(self, account: SourcePrincipal, item_keys: Sequence[str]) -> dict[str, bool]:
        """For each item key, whether ``account`` may read it according to the source."""
        ...
