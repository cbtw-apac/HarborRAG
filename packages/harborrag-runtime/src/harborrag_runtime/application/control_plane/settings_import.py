"""Import, export and first-boot seeding of runtime settings files.

``SettingsImporter`` plans a document against a tenant's stored overrides and applies
the plan as one revision; ``seed_settings_once`` uses it at API start. The CLI
``config import`` and ``config export`` use the same importer.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, get_args

from harborrag_core.config_store import SettingsLevel, SettingsRegistry, SettingsSnapshot
from harborrag_core.config_store.documents import (
    ImportPlan,
    SettingsDocument,
    drift_keys,
    export_document,
    plan_import,
)
from harborrag_core.config_store.levels import LevelKind
from harborrag_core.contracts.errors import (
    HarborPreconditionFailedError,
    HarborUnavailableError,
    HarborValidationError,
)
from harborrag_core.observability.log_events import FailureKind, log_event, log_failure
from harborrag_core.ports.config_store import SettingsStore
from harborrag_runtime.composition import ControlPlaneRepositories
from harborrag_runtime.config.settings_document import (
    SettingsDocumentError,
    load_settings_document,
)

from ..errors import SettingsLevelNotFoundError
from .settings_service import require_settings_level

logger = logging.getLogger("harborrag.runtime.application.control_plane.settings_import")

SeedOutcome = Literal["seeded", "drift", "in_sync", "no_file", "invalid", "raced"]
SEED_ACTOR = "system"
SEED_INVALID_EVENT = "control_plane.settings_seed_invalid"


@dataclass(frozen=True, slots=True)
class SettingsExport:
    """An exported settings document and the levels left out of it.

    Attributes:
        document: Stored overrides of the levels that still exist.
        left_out: Project and connection levels whose project or connection is gone.
    """

    document: SettingsDocument
    left_out: tuple[str, ...]


class SettingsImporter:
    """Plan and apply settings documents for one tenant at a time.

    Args:
        control_plane: Returns the control-plane repositories.
        registry: Registry of the settings keys and their validators.
    """

    def __init__(
        self, control_plane: Callable[[], ControlPlaneRepositories], registry: SettingsRegistry
    ) -> None:
        self._control_plane = control_plane
        self._registry = registry

    async def snapshot(self, *, tenant_id: str) -> SettingsSnapshot:
        """Return the tenant's stored overrides.

        Args:
            tenant_id: Tenant to read.

        Returns:
            The tenant's overrides at its current revision.

        Raises:
            HarborUnavailableError: The settings store is not configured.
        """
        return await self._store().load(tenant_id)

    async def plan(self, *, tenant_id: str, document: SettingsDocument, prune: bool) -> ImportPlan:
        """Plan the changes that make the tenant's overrides match ``document``.

        Args:
            tenant_id: Tenant to plan for.
            document: Overrides the file declares.
            prune: Also remove stored overrides the document lacks.

        Returns:
            The plan, made against the tenant's current revision.

        Raises:
            SettingsLevelNotFoundError: A project or connection of the document does not exist.
            HarborValidationError: A key or value is invalid (``field``, ``level``).
            HarborUnavailableError: The settings store is not configured.
        """
        snapshot = await self.snapshot(tenant_id=tenant_id)
        for label in document.levels:
            try:
                await require_settings_level(
                    self._control_plane(), tenant_id, SettingsLevel.parse(label)
                )
            except SettingsLevelNotFoundError as error:
                raise SettingsLevelNotFoundError(
                    str(error), details={"field": "level", "level": label}
                ) from error
        return plan_import(self._registry, snapshot, document, prune=prune)

    async def apply(
        self, plan: ImportPlan, *, source: Literal["seed", "import"], created_by: str
    ) -> int | None:
        """Write ``plan`` as the next revision and invalidate the cache.

        Args:
            plan: Plan to write.
            source: Origin recorded in the revision log.
            created_by: Actor recorded in the revision log.

        Returns:
            The new revision, or ``None`` when the plan is empty.

        Raises:
            HarborPreconditionFailedError: The tenant changed since the plan was made.
            HarborUnavailableError: The settings store is not configured.
        """
        if plan.empty:
            return None
        try:
            return await self._store().write(
                plan.tenant_id,
                plan.changes,
                expected_revision=plan.base_revision,
                source=source,
                created_by=created_by,
            )
        finally:
            config = self._control_plane().config
            if config is not None:
                config.invalidate(plan.tenant_id)

    async def export(self, *, tenant_id: str, level: SettingsLevel | None) -> SettingsExport:
        """Return the stored overrides of ``level``, or of every level when ``None``.

        Only registry keys leave through an export; any other stored key is left out. So
        is a level whose project or connection was deleted, since importing it would fail.
        """
        document = export_document(await self.snapshot(tenant_id=tenant_id), level)
        known = {
            label: {key: value for key, value in body.items() if key in self._registry.definitions}
            for label, body in document.levels.items()
        }
        kept: dict[str, dict[str, object]] = {}
        left_out: list[str] = []
        for label, body in known.items():
            if not body:
                continue
            if await self._level_exists(tenant_id, label):
                kept[label] = body
            else:
                left_out.append(label)
        return SettingsExport(SettingsDocument(kept), tuple(left_out))

    async def _level_exists(self, tenant_id: str, label: str) -> bool:
        """Whether the project or connection of level ``label`` exists in the tenant."""
        try:
            await require_settings_level(
                self._control_plane(), tenant_id, SettingsLevel.parse(label)
            )
        except SettingsLevelNotFoundError:
            return False
        return True

    def drift(self, snapshot: SettingsSnapshot, document: SettingsDocument) -> tuple[str, ...]:
        """Return the keys where ``document`` and the stored overrides differ."""
        return drift_keys(self._registry, snapshot, document)

    def _store(self) -> SettingsStore:
        """Return the settings store, or raise ``HarborUnavailableError`` without one."""
        store = self._control_plane().config_store
        if store is None:
            raise HarborUnavailableError("settings store is not configured")
        return store


async def seed_settings_once(
    importer: SettingsImporter, path: Path, *, tenant_id: str
) -> SeedOutcome:
    """Seed ``tenant_id`` from the settings file when its log is empty, else report drift.

    Args:
        importer: Importer of the tenant's store.
        path: Settings file; a missing file seeds nothing.
        tenant_id: Tenant to seed.

    Returns:
        What happened. An invalid file is logged at ERROR and nothing is written; a
        lost race with another replica is logged at INFO.
    """
    if not path.is_file():
        return "no_file"
    try:
        document = load_settings_document(path)
        snapshot = await importer.snapshot(tenant_id=tenant_id)
        if snapshot.revision > 0:
            return _report_drift(importer, snapshot, document, path)
        plan = await importer.plan(tenant_id=tenant_id, document=document, prune=False)
        if plan.skipped:
            log_event(
                logger,
                "control_plane.settings_seed_skipped",
                level=logging.WARNING,
                config_path=str(path),
                keys_count=len({item.key for item in plan.skipped}),
                skip_reason=",".join(sorted({item.reason for item in plan.skipped})),
                hint="Run harborrag settings import on the file to list the skipped keys.",
            )
        if await importer.apply(plan, source="seed", created_by=SEED_ACTOR) is None:
            return "in_sync"
    except SettingsDocumentError as error:
        log_failure(
            logger,
            SEED_INVALID_EVENT,
            error,
            FailureKind.CLASSIFIED,
            config_path=str(path),
            settings_level_kind=_level_kind(error.level),
        )
        return "invalid"
    except HarborValidationError as error:
        log_failure(
            logger,
            SEED_INVALID_EVENT,
            error,
            FailureKind.CLASSIFIED,
            config_path=str(path),
            settings_level_kind=_level_kind(error.details.get("level")),
        )
        return "invalid"
    except HarborPreconditionFailedError:
        log_event(logger, "control_plane.settings_seed_raced", config_path=str(path))
        return "raced"
    log_event(logger, "control_plane.settings_seeded", config_path=str(path), tenant_id=tenant_id)
    return "seeded"


def _report_drift(
    importer: SettingsImporter, snapshot: SettingsSnapshot, document: SettingsDocument, path: Path
) -> SeedOutcome:
    """Warn when the file and the stored overrides differ; the stored ones stay in force."""
    keys = importer.drift(snapshot, document)
    if not keys:
        return "in_sync"
    log_event(
        logger,
        "control_plane.settings_file_drift",
        level=logging.WARNING,
        config_path=str(path),
        keys_count=len(keys),
        hint=(
            "The stored settings stay in force; run harborrag settings import on the file "
            "to list the differing keys, with --apply to store the file's values."
        ),
    )
    return "drift"


def _level_kind(label: object) -> str | None:
    """Return the kind of a settings level label, such as ``project``; None when unknown."""
    if not isinstance(label, str):
        return None
    kind = label.partition(":")[0]
    return kind if kind in get_args(LevelKind) else None
