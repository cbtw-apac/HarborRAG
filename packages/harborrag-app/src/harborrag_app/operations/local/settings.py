"""In-process runtime settings operations.

The transport sets the ``ETag`` header and status codes.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from harborrag_app.operations.contract import OPERATIONS
from harborrag_app.operations.etags import etag, version_from_etag
from harborrag_app.operations.results import Tagged
from harborrag_app.operations.schemas.settings import SettingsOut, SettingsPatchOut
from harborrag_app.operations.scope import Scope
from harborrag_core.config_store import revision_label
from harborrag_runtime.application.control_plane.settings_service import SettingsPatch

if TYPE_CHECKING:
    from harborrag_runtime.application.composition.service import AppService


class SettingsOps:
    """The settings operations of ``LocalOperations``.

    ``LocalOperations`` sets ``_app_service``, whose settings service runs them.
    """

    _app_service: AppService

    async def read_settings(
        self, scope: Scope, *, level: str | None, effective: bool
    ) -> Tagged[SettingsOut]:
        """Read the overrides stored at a level, or every key's effective value and origin.

        A connection's level needs ``editor``; every other level needs ``admin``.

        Args:
            scope: Caller and tenant.
            level: ``tenant`` (``None``), ``project:<id>`` or ``connection:<project_id>/<id>``.
            effective: Resolve every key through the level chain instead of listing overrides.

        Returns:
            The values and the tenant revision they were read at, with that revision's ETag.

        Raises:
            HarborValidationError: ``level`` is malformed.
            HarborAuthError: The caller is below ``admin`` for a level that is not a connection's.
            SettingsLevelNotFoundError: The project or connection of ``level`` does not exist.
        """
        scope.require(OPERATIONS["read_settings"].role)
        result = await self._app_service.settings_service.read(
            tenant_id=scope.tenant_id,
            level=level,
            effective=effective,
            role=scope.principal.role,
        )
        body = SettingsOut(
            level=result.level, revision=revision_label(result.revision), values=result.values
        )
        return Tagged(body, etag(result.revision))

    async def patch_settings(  # noqa: PLR0913 - body, level and the patch's flags
        self,
        scope: Scope,
        body: Mapping[str, object],
        *,
        level: str | None,
        dry_run: bool,
        reindex: bool,
        if_match: str | None,
    ) -> Tagged[SettingsPatchOut]:
        """Save changes at a level as the next revision; nothing is saved when a key is refused.

        Args:
            scope: Caller and tenant; the caller is recorded as the author.
            body: Key to new value; ``None`` removes the override.
            level: ``tenant`` (``None``), ``project:<id>`` or ``connection:<project_id>/<id>``.
            dry_run: Plan the changes without saving them.
            reindex: Always refused; a settings change cannot start a reindex.
            if_match: ETag from the caller's last read, or ``*``; required.

        Returns:
            The revision after the call and the planned or applied changes, with its ETag.

        Raises:
            HarborValidationError: ``level`` is malformed, ``reindex`` is set, ``if_match`` is
                missing, or a key or value of ``body`` is invalid.
            HarborPreconditionFailedError: The settings changed since ``if_match`` was read.
            SettingsReindexRequiredError: A key of ``body`` needs a reindex.
        """
        scope.require(OPERATIONS["patch_settings"].role)
        result = await self._app_service.settings_service.patch(
            tenant_id=scope.tenant_id,
            request=SettingsPatch(
                level=level,
                body=body,
                expected_revision=version_from_etag(if_match),
                dry_run=dry_run,
                reindex=reindex,
            ),
            actor=scope.actor,
        )
        patched = SettingsPatchOut.model_validate(
            {
                "revision": revision_label(result.revision),
                "dry_run": result.dry_run,
                "changed": [change.as_dict() for change in result.changed],
            }
        )
        return Tagged(patched, etag(result.revision))

    async def settings_schema(self, scope: Scope) -> dict[str, Any]:
        """Describe every setting by group: type, range, levels, when a change applies."""
        scope.require(OPERATIONS["settings_schema"].role)
        return self._app_service.settings_service.schema()
