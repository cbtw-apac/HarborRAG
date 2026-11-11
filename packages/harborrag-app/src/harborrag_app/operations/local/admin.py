"""In-process identity, source-type, connection and project operations.

Handles:
- ``If-Match`` and ``If-None-Match`` values as the services' expected version and create-only
- Request bodies as ``ConnectionWrite``, service tuples as response models with their ETags

Does NOT choose status codes or headers; the transport does.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, get_args

from harborrag_app.api.auth.principal import Principal, authorize_role
from harborrag_app.api.v1.admin.connection_schemas import (
    ConnectionChipPage,
    ConnectionOut,
    ConnectionPage,
    ConnectionTestOut,
    ConnectionTestRequest,
    ConnectionWriteRequest,
    JobAcceptedOut,
    ProjectOut,
    ProjectPage,
    ProjectWriteRequest,
    SourceTypePage,
)
from harborrag_app.api.v1.me_schemas import MeOut, MePrincipal
from harborrag_app.operations.contract import OPERATIONS
from harborrag_app.operations.results import Saved, Tagged
from harborrag_app.operations.scope import ConnectionRef, Scope
from harborrag_app.workflow_control.control_plane.connection_views import (
    etag,
    version_from_etag,
)
from harborrag_app.workflow_control.control_plane.connections import ConnectionWrite, WriteOrigin
from harborrag_core.contracts.errors import HarborValidationError
from harborrag_core.domain.connection import ConnectionStatus
from harborrag_runtime.config.connectors.models import source_types
from harborrag_runtime.config.connectors.models.spec import Category

if TYPE_CHECKING:
    from harborrag_app.workflow_control.composition.service import AppService


class AdminOps:
    """The identity, source-type, connection and project operations of ``LocalOperations``.

    ``LocalOperations`` sets ``_app_service``, whose connection and project services run them.
    """

    _app_service: AppService

    async def me(self, principal: Principal, *, tenant_id: str | None) -> MeOut:
        """Describe the credential, so a client can pick its screens and its tenant.

        Args:
            principal: The verified caller.
            tenant_id: The tenant picked for the call; ``None`` when several are held and
                none was named.

        Returns:
            The principal, the picked tenant, every tenant held and the role.

        Raises:
            TenantForbiddenError: The principal does not hold ``tenant_id``.
        """
        if tenant_id is None:
            authorize_role(principal, OPERATIONS["me"].role)
        else:
            Scope(principal, tenant_id).require(OPERATIONS["me"].role)
        return MeOut(
            principal=MePrincipal(
                principal_type=principal.kind,  # type: ignore[arg-type]  # api_key or user
                principal_id=principal.subject,
                name=principal.name or principal.subject,
            ),
            tenant_id=tenant_id,
            tenants=sorted(principal.tenant_ids),
            role=principal.role,
        )

    async def list_source_types(self, scope: Scope, *, category: str | None) -> SourceTypePage:
        """List each source type with its credential and config schemas and form hints.

        Args:
            scope: Caller and tenant.
            category: ``issue_tracker``, ``wiki``, ``files`` or ``code``; ``None`` for all.

        Returns:
            The matching types.

        Raises:
            HarborValidationError: ``category`` is not one of the four categories.
        """
        scope.require(OPERATIONS["list_source_types"].role)
        selected = _category(category)
        items = [source_types.describe(spec.source_type) for spec in source_types.all(selected)]
        return SourceTypePage.model_validate({"items": items})

    async def list_connections(  # noqa: PLR0913 - the route's list filters plus paging
        self,
        scope: Scope,
        *,
        project_id: str | None,
        source_type: str | None,
        status: ConnectionStatus | None,
        limit: int,
        cursor: str | None,
    ) -> ConnectionPage:
        """List the tenant's connections, the ones that need attention first.

        Args:
            scope: Caller and tenant.
            project_id: Only this project's connections, when set.
            source_type: Only connections of this type, when set.
            status: Only connections in this status, when set.
            limit: Page size.
            cursor: ``next_cursor`` of the previous page.

        Returns:
            One page and the cursor of the next, ``None`` on the last page.
        """
        scope.require(OPERATIONS["list_connections"].role)
        items, next_cursor = await self._app_service.connection_service.list_page(
            tenant_id=scope.tenant_id,
            project_id=project_id,
            source_type=source_type,
            status=status,
            limit=limit,
            cursor=cursor,
        )
        return ConnectionPage.model_validate({"items": items, "next_cursor": next_cursor})

    async def get_connection(self, scope: Scope, ref: ConnectionRef) -> Tagged[ConnectionOut]:
        """Read one connection; secrets read back as their status, never the value.

        Args:
            scope: Caller and tenant.
            ref: The connection to read.

        Returns:
            The connection and its ETag.

        Raises:
            HarborNotFoundError: No such connection in the tenant.
        """
        scope.require(OPERATIONS["get_connection"].role)
        view, version = await self._app_service.connection_service.get(ref.key(scope.tenant_id))
        return Tagged(ConnectionOut.model_validate(view), etag(version))

    async def put_connection(  # noqa: PLR0913 - body, preconditions and write origin
        self,
        scope: Scope,
        ref: ConnectionRef,
        body: ConnectionWriteRequest,
        *,
        if_match: str | None,
        if_none_match: str | None,
        origin: WriteOrigin = "api",
    ) -> Saved[ConnectionOut]:
        """Create or overwrite a connection, checked against its source type's schemas.

        Args:
            scope: Caller and tenant; the caller is recorded as the author.
            ref: The connection to write.
            body: Source type, name, credentials, config and ``advanced`` tuning.
            if_match: ETag the stored connection must still carry, or ``*``.
            if_none_match: ``*`` makes the write create-only.
            origin: ``operator`` lets the write set new ``env:`` references.

        Returns:
            The stored connection, its new ETag and whether it was created.

        Raises:
            HarborPreconditionFailedError: ``if_match`` or ``if_none_match`` does not hold.
        """
        scope.require(OPERATIONS["put_connection"].role)
        result = await self._app_service.connection_service.put(
            ref.key(scope.tenant_id),
            _connection_write(body),
            actor=scope.actor,
            expected_version=version_from_etag(if_match),
            create_only=if_none_match == "*",
            origin=origin,
        )
        return Saved(
            ConnectionOut.model_validate(result.connection), etag(result.version), result.created
        )

    async def delete_connection(
        self, scope: Scope, ref: ConnectionRef, *, purge: bool
    ) -> JobAcceptedOut:
        """Remove the connection's documents, secrets and row in the background.

        Args:
            scope: Caller and tenant.
            ref: The connection to remove.
            purge: Also drop its Postgres history.

        Returns:
            The removal job.
        """
        scope.require(OPERATIONS["delete_connection"].role)
        job_id = await self._app_service.connection_service.delete(
            ref.key(scope.tenant_id), actor=scope.actor, purge=purge
        )
        return JobAcceptedOut(job_id=job_id)

    async def test_connection(
        self,
        scope: Scope,
        ref: ConnectionRef,
        body: ConnectionTestRequest | None,
        *,
        origin: WriteOrigin = "api",
    ) -> ConnectionTestOut:
        """Check form values, or the saved connection without a body; saves nothing.

        Args:
            scope: Caller and tenant.
            ref: The connection the values belong to.
            body: Form values to check; ``None`` checks the saved connection.
            origin: ``operator`` lets the values name new ``env:`` references.

        Returns:
            Each check's outcome; ``ok`` is false when one failed.
        """
        scope.require(OPERATIONS["test_connection"].role)
        write = _connection_write(body) if body is not None else None
        report = await self._app_service.connection_service.test(
            ref.key(scope.tenant_id), write, origin=origin
        )
        return ConnectionTestOut.model_validate(report)

    async def list_projects(
        self, scope: Scope, *, q: str | None, limit: int, cursor: str | None
    ) -> ProjectPage:
        """List the projects in the tenant.

        Args:
            scope: Caller and tenant.
            q: Name search, when set.
            limit: Page size.
            cursor: ``next_cursor`` of the previous page.

        Returns:
            One page and the cursor of the next, ``None`` on the last page.
        """
        scope.require(OPERATIONS["list_projects"].role)
        items, next_cursor = await self._app_service.project_service.list_page(
            tenant_id=scope.tenant_id, query=q, limit=limit, cursor=cursor
        )
        return ProjectPage.model_validate({"items": items, "next_cursor": next_cursor})

    async def get_project(self, scope: Scope, project_id: str) -> Tagged[ProjectOut]:
        """Read one project.

        Args:
            scope: Caller and tenant.
            project_id: The project to read.

        Returns:
            The project and its ETag.

        Raises:
            HarborNotFoundError: No such project in the tenant.
        """
        scope.require(OPERATIONS["get_project"].role)
        view, version = await self._app_service.project_service.get(
            tenant_id=scope.tenant_id, project_id=project_id
        )
        return Tagged(ProjectOut.model_validate(view), etag(version))

    async def put_project(
        self,
        scope: Scope,
        project_id: str,
        body: ProjectWriteRequest,
        *,
        if_match: str | None,
        if_none_match: str | None,
    ) -> Saved[ProjectOut]:
        """Create or update a project by the id the caller chooses.

        Args:
            scope: Caller and tenant; the caller is recorded as the author.
            project_id: Id of the project, chosen by the caller.
            body: Its name and description.
            if_match: ETag the stored project must still carry, or ``*``.
            if_none_match: ``*`` makes the write create-only.

        Returns:
            The stored project, its new ETag and whether it was created.

        Raises:
            HarborPreconditionFailedError: ``if_match`` or ``if_none_match`` does not hold.
        """
        scope.require(OPERATIONS["put_project"].role)
        view, created, version = await self._app_service.project_service.put(
            tenant_id=scope.tenant_id,
            project_id=project_id,
            name=body.project_name,
            description=body.project_description,
            actor=scope.actor,
            expected_version=version_from_etag(if_match),
            create_only=if_none_match == "*",
        )
        return Saved(ProjectOut.model_validate(view), etag(version), created)

    async def delete_project(
        self, scope: Scope, project_id: str, *, cascade: bool
    ) -> JobAcceptedOut | None:
        """Delete an empty project at once, or with ``cascade`` in the background.

        Args:
            scope: Caller and tenant.
            project_id: The project to delete.
            cascade: Also remove its connections, documents and secrets.

        Returns:
            ``None`` when the empty project is gone, else the cascade's job.

        Raises:
            HarborInUseError: It has connections and ``cascade`` is false.
        """
        scope.require(OPERATIONS["delete_project"].role)
        job_id = await self._app_service.project_service.delete(
            tenant_id=scope.tenant_id, project_id=project_id, actor=scope.actor, cascade=cascade
        )
        return None if job_id is None else JobAcceptedOut(job_id=job_id)

    async def list_searchable_connections(
        self, scope: Scope, project_id: str
    ) -> ConnectionChipPage:
        """List the connections the caller can search in a project, for filter chips.

        Args:
            scope: Caller and tenant.
            project_id: The project whose connections to list.

        Returns:
            Each connection that is not being deleted, with its document count.

        Raises:
            HarborNotFoundError: The project does not exist in the tenant.
        """
        scope.require(OPERATIONS["list_searchable_connections"].role)
        items = await self._app_service.connection_service.searchable(
            tenant_id=scope.tenant_id, project_id=project_id
        )
        return ConnectionChipPage.model_validate({"items": items})


def _category(value: str | None) -> Category | None:
    """Narrow a category name to ``Category``; ``None`` stays ``None``."""
    if value is None:
        return None
    for category in get_args(Category):
        if value == category:
            return category  # type: ignore[no-any-return]  # get_args yields the Literal's members
    raise HarborValidationError(
        f"category must be one of {', '.join(get_args(Category))}", {"field": "category"}
    )


def _connection_write(body: ConnectionWriteRequest | ConnectionTestRequest) -> ConnectionWrite:
    """Build the service's write from a ``PUT`` or ``test`` body; ``advanced`` becomes ``tuning``."""
    return ConnectionWrite(
        source_type=body.source_type,
        credentials=_credentials(body.credentials),
        config=body.config,
        name=body.name,
        tuning=body.advanced,
    )


def _credentials(values: dict[str, Any]) -> dict[str, Any]:
    """Turn request models back into the plain mapping the service takes."""
    return {
        name: value.model_dump() if hasattr(value, "model_dump") else value
        for name, value in values.items()
    }
