"""Import connections from YAML and export them back.

Handles:
- ``plan``: read a legacy ``connectors.yaml`` (v1) or an export document and
  list what would change, writing nothing
- ``apply``: run a plan; an unchanged connection is never written again
- ``export``: one project's connections as YAML, secrets as references or status
- ``import_connectors_once``: the one-time import at first start

A legacy entry's key becomes its ``source_key`` (and ``connection_id`` when the
key is a valid id), so documents ingested before the upgrade keep their ids. An
entry that fails its schema, or names an unset environment variable, is still
imported, with status ``error`` and its field issues, so nothing is lost.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Literal

import yaml

from harborrag_core.base import utc_now
from harborrag_core.domain.connection import (
    CONNECTION_ID_PATTERN,
    SYSTEM_ACTOR,
    Connection,
    ConnectionBuilder,
    slugify_connection_id,
)
from harborrag_core.domain.project import Project
from harborrag_core.ports.control_plane import (
    ConnectionRepositoryPort,
    ProjectRepositoryPort,
    SettingsRepositoryPort,
)
from harborrag_core.security.secret_refs import (
    REFERENCE_KEY,
    SecretRef,
    StoredSecret,
    all_refs,
)

from .models import ConnectionValidationError, FieldIssue, SourceTypeSpec, source_types
from .parsing import parse_connector_definitions
from .providers import coerce_config_values
from .schemas import ConnectorDefinition

logger = logging.getLogger("harborrag.runtime.config.connectors.importing")

DEFAULT_PROJECT_ID = "default"
EXPORT_VERSION = 2
IMPORT_MARKER_KEY = "connections_import"
IMPORT_ACTOR = "import"

ChangeAction = Literal["create", "update", "unchanged", "skip", "invalid"]


@dataclass(frozen=True, slots=True)
class ImportChange:
    """What the import will do with one entry."""

    action: ChangeAction
    source_key: str
    project_id: str
    connection_id: str
    source_type: str | None = None
    reason: str | None = None
    connection: Connection | None = None

    def as_dict(self) -> dict[str, Any]:
        """Return the change without the connection it would write."""
        return {
            "action": self.action,
            "source_key": self.source_key,
            "project_id": self.project_id,
            "connection_id": self.connection_id,
            "source_type": self.source_type,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class ImportPlan:
    """Every change an import would make, ready to inspect before ``apply``."""

    tenant_id: str
    changes: tuple[ImportChange, ...]
    projects_to_create: tuple[str, ...] = ()

    @property
    def writes(self) -> tuple[ImportChange, ...]:
        """Changes that create or update a connection."""
        return tuple(change for change in self.changes if change.action in {"create", "update"})

    def summary(self) -> dict[str, int]:
        """Count of changes per action."""
        counts: dict[str, int] = {}
        for change in self.changes:
            counts[change.action] = counts.get(change.action, 0) + 1
        return counts


@dataclass(frozen=True, slots=True)
class ImportResult:
    """What ``apply`` wrote."""

    created: int = 0
    updated: int = 0
    projects_created: tuple[str, ...] = field(default_factory=tuple)


class ConnectionImportService:
    """Plan, apply and export connection documents for one tenant at a time."""

    def __init__(
        self,
        *,
        projects: ProjectRepositoryPort,
        connections: ConnectionRepositoryPort,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self._projects = projects
        self._connections = connections
        self._environ = os.environ if environ is None else environ

    async def plan(
        self,
        document: str | Mapping[str, Any],
        *,
        tenant_id: str,
        base_directory: Path | None = None,
    ) -> ImportPlan:
        """List what importing ``document`` would change; nothing is written.

        Args:
            document: YAML text or its parsed mapping, legacy v1 or export format.
            tenant_id: Tenant the connections belong to.
            base_directory: Directory relative local paths resolve against.

        Returns:
            One change per entry, plus the projects that would be created.

        Raises:
            ValueError: The document is neither format.
        """
        raw = yaml.safe_load(document) if isinstance(document, str) else document
        if not isinstance(raw, Mapping):
            raise ValueError("connection document must be a mapping")
        if isinstance(raw.get("connections"), list):
            desired = [self._from_export(entry, tenant_id) for entry in raw["connections"]]
        elif isinstance(raw.get("connectors"), Mapping):
            desired = self._from_legacy(raw["connectors"], tenant_id, base_directory or Path.cwd())
        else:
            raise ValueError("expected a 'connectors' mapping or a 'connections' list")
        changes = tuple([await self._compare(change) for change in desired])
        missing_projects = sorted(
            {
                change.project_id
                for change in changes
                if change.action == "create"
                and await self._projects.get(change.project_id, tenant_ids=frozenset({tenant_id}))
                is None
            }
        )
        return ImportPlan(tenant_id, changes, tuple(missing_projects))

    async def apply(self, plan: ImportPlan) -> ImportResult:
        """Create missing projects, then write every create and update of ``plan``."""
        for project_id in plan.projects_to_create:
            await self._projects.create(
                Project(
                    id=project_id,
                    name=project_id.capitalize(),
                    collection=project_id,
                    tenant_id=plan.tenant_id,
                )
            )
        created = updated = 0
        for change in plan.writes:
            if change.connection is None:
                continue
            await self._connections.put(change.connection)
            if change.action == "create":
                created += 1
            else:
                updated += 1
        logger.info(
            "Connection import applied created=%d updated=%d projects=%d",
            created,
            updated,
            len(plan.projects_to_create),
        )
        return ImportResult(created, updated, plan.projects_to_create)

    async def export(self, *, tenant_id: str, project_id: str) -> str:
        """Return a project's connections as an importable YAML document.

        Secrets read back as their ``secret_ref`` when they point outside
        HarborRAG, and as their status when HarborRAG stores the value.
        """
        connections, _ = await self._connections.list(
            tenant_ids=frozenset({tenant_id}), project_id=project_id
        )
        entries = [
            {
                "project_id": connection.project_id,
                "connection_id": connection.connection_id,
                "source_key": connection.source_key,
                "source_type": connection.source_type,
                "name": connection.name,
                "credentials": _exported_credentials(connection.credentials),
                "config": dict(connection.config),
                "tuning": dict(connection.tuning),
            }
            for connection in connections
        ]
        return yaml.safe_dump({"version": EXPORT_VERSION, "connections": entries}, sort_keys=False)

    def _from_legacy(
        self, raw: Mapping[str, Any], tenant_id: str, base_directory: Path
    ) -> list[ImportChange]:
        """Turn legacy ``connectors`` entries into desired connections."""
        projects = {
            str(name): str(entry.get("project_id") or DEFAULT_PROJECT_ID)
            for name, entry in raw.items()
            if isinstance(entry, Mapping)
        }
        stripped = {
            name: {key: value for key, value in entry.items() if key != "project_id"}
            if isinstance(entry, Mapping)
            else entry
            for name, entry in raw.items()
        }
        definitions, errors = parse_connector_definitions(stripped, base_directory=base_directory)
        changes = [
            _invalid(str(name), projects.get(str(name), DEFAULT_PROJECT_ID), str(error))
            for name, error in errors.items()
        ]
        for name, definition in definitions.items():
            project_id = projects.get(name, DEFAULT_PROJECT_ID)
            if not definition.enabled:
                changes.append(
                    ImportChange("skip", name, project_id, _connection_id(name), reason="disabled")
                )
                continue
            changes.append(self._desired(tenant_id, project_id, name, definition))
        return changes

    def _desired(
        self, tenant_id: str, project_id: str, name: str, definition: ConnectorDefinition
    ) -> ImportChange:
        """Build the connection one legacy entry describes; problems become field issues."""
        spec = source_types.get(definition.provider)
        flat, missing = self._flat_values(spec, definition)
        credentials, config, tuning = spec.split_flat(flat)
        return self._validated(
            tenant_id=tenant_id,
            project_id=project_id,
            source_key=name,
            connection_id=_connection_id(name),
            spec=spec,
            name=name,
            parts=(credentials, config, tuning),
            issues=[
                FieldIssue(
                    _field_path(spec, field_name), f"environment variable {variable} is not set"
                )
                for field_name, variable in missing
            ],
        )

    def _flat_values(
        self, spec: SourceTypeSpec, definition: ConnectorDefinition
    ) -> tuple[dict[str, Any], list[tuple[str, str]]]:
        """Dataclass keywords of an entry, and the environment variables it names but lacks.

        Environment values are read now; secrets stay ``env:`` references.
        """
        values: dict[str, Any] = dict(definition.settings)
        from_environment: set[str] = set()
        missing: list[tuple[str, str]] = []
        secret_fields = source_types.secret_dataclass_fields(spec.source_type)
        references = {
            name: variable
            for name, variable in definition.secret_environment.items()
            if name in secret_fields
        }
        read = {
            **definition.setting_environment,
            **{
                name: variable
                for name, variable in definition.secret_environment.items()
                if name not in secret_fields
            },
        }
        for name, variable in read.items():
            if self._environ.get(variable):
                values[name] = self._environ[variable]
                from_environment.add(name)
            else:
                missing.append((name, variable))
        values.update(
            {name: {REFERENCE_KEY: f"env:{variable}"} for name, variable in references.items()}
        )
        if "source_path" in values and isinstance(values["source_path"], str):
            path = Path(values["source_path"]).expanduser()
            if not path.is_absolute():
                base = (
                    Path.cwd() if "source_path" in from_environment else definition.base_directory
                )
                values["source_path"] = str((base / path).resolve())
        factory = source_types.provider_factory(spec.source_type)
        plain = {key: value for key, value in values.items() if not isinstance(value, Mapping)}
        return {**values, **coerce_config_values(factory, plain)}, missing

    def _from_export(self, entry: Mapping[str, Any], tenant_id: str) -> ImportChange:
        """Turn one export entry into a desired connection."""
        source_key = str(entry.get("source_key") or entry.get("connection_id") or "")
        project_id = str(entry.get("project_id") or DEFAULT_PROJECT_ID)
        connection_id = str(entry.get("connection_id") or _connection_id(source_key))
        try:
            spec = source_types.get(str(entry.get("source_type", "")))
        except ConnectionValidationError:
            return _invalid(source_key, project_id, "unknown source type")
        credentials = {
            key: value
            for key, value in dict(entry.get("credentials") or {}).items()
            if not (isinstance(value, Mapping) and "set" in value)
        }
        return self._validated(
            tenant_id=tenant_id,
            project_id=project_id,
            source_key=source_key,
            connection_id=connection_id,
            spec=spec,
            name=str(entry.get("name") or connection_id),
            parts=(credentials, dict(entry.get("config") or {}), dict(entry.get("tuning") or {})),
        )

    def _validated(  # noqa: PLR0913 - the identity and parts of one connection
        self,
        *,
        tenant_id: str,
        project_id: str,
        source_key: str,
        connection_id: str,
        spec: SourceTypeSpec,
        name: str,
        parts: tuple[dict[str, Any], dict[str, Any], dict[str, Any]],
        issues: list[FieldIssue] | None = None,
    ) -> ImportChange:
        """Build the connection to write; one that fails its schema is kept with status error."""
        credentials, config, tuning = parts
        problems = list(issues or [])
        try:
            validated = spec.validate(credentials, config, tuning)
            credentials, config, tuning = validated.credentials, validated.config, validated.tuning
        except ConnectionValidationError as error:
            problems.extend(error.issues)
        now = utc_now()
        stored = {
            key: StoredSecret.for_reference(SecretRef.parse(value[REFERENCE_KEY]), now).to_json()
            if isinstance(value, Mapping) and REFERENCE_KEY in value
            else value
            for key, value in credentials.items()
        }
        builder = (
            ConnectionBuilder.for_new(
                tenant_id=tenant_id, project_id=project_id, connection_id=connection_id
            )
            .source_type(spec.source_type)
            .source_key(source_key)
            .name(name)
            .credentials(stored)
            .config(config)
            .tuning(tuning)
            .secret_refs(all_refs(stored))
            .actor(IMPORT_ACTOR)
        )
        reason = None
        if problems:
            builder.status("error", {"errors": [problem.as_dict() for problem in problems]})
            reason = "; ".join(f"{problem.field}: {problem.issue}" for problem in problems)
        return ImportChange(
            "create",
            source_key,
            project_id,
            connection_id,
            spec.source_type,
            reason=reason,
            connection=builder.build(now=now),
        )

    async def _compare(self, change: ImportChange) -> ImportChange:
        """Turn a desired connection into create, update or unchanged against what is stored."""
        if change.connection is None:
            return change
        desired = change.connection
        existing = await self._connections.get_by_source_key(
            tenant_id=desired.tenant_id, source_key=desired.source_key
        )
        if existing is None:
            return change
        if _same_content(existing, desired):
            return replace(change, action="unchanged", connection=None)
        updated = (
            ConnectionBuilder.from_existing(existing)
            .name(desired.name)
            .credentials(desired.credentials)
            .config(desired.config)
            .tuning(desired.tuning)
            .secret_refs(desired.secret_refs)
            .actor(IMPORT_ACTOR)
            .build()
        )
        return replace(
            change,
            action="update",
            project_id=existing.project_id,
            connection_id=existing.connection_id,
            connection=updated,
        )


async def import_connectors_once(  # noqa: PLR0913 - the stores and the file it reads once
    path: Path,
    *,
    projects: ProjectRepositoryPort,
    connections: ConnectionRepositoryPort,
    settings: SettingsRepositoryPort,
    environ: Mapping[str, str] | None = None,
) -> ImportPlan | None:
    """Import ``path`` the first time an install starts with no connections.

    It runs only when the file exists, the install has at most one tenant, no
    connection a person created (rows migration 0041 carried over from
    ``/api/v1/sources`` do not count), and no earlier import was recorded. The
    outcome is recorded in the workspace settings so the file is never read again.

    Returns:
        The applied plan, or None when the import did not run.
    """
    workspace = await settings.get()
    if IMPORT_MARKER_KEY in workspace.data or not path.is_file():
        return None
    existing, _ = await connections.list(tenant_ids=None)
    tenants = {project.tenant_id for project in await projects.list(tenant_ids=None)}
    if any(connection.created_by != SYSTEM_ACTOR for connection in existing) or len(tenants) > 1:
        return None
    tenant_id = next(iter(tenants), "DEFAULT")
    service = ConnectionImportService(projects=projects, connections=connections, environ=environ)
    plan = await service.plan(
        path.read_text(encoding="utf-8"), tenant_id=tenant_id, base_directory=path.parent
    )
    for change in plan.changes:
        logger.info("Connection import plan %s", change.as_dict())
    result = await service.apply(plan)
    await settings.put(
        replace(
            workspace,
            data={
                **workspace.data,
                IMPORT_MARKER_KEY: {
                    "at": utc_now().isoformat(),
                    "source": str(path),
                    "created": result.created,
                    "skipped": plan.summary().get("skip", 0),
                    "invalid": plan.summary().get("invalid", 0),
                    "errors": sum(1 for change in plan.writes if change.reason),
                },
            },
        )
    )
    return plan


def _field_path(spec: SourceTypeSpec, dataclass_field: str) -> str:
    """Name a dataclass keyword the way validation issues name fields."""
    public = {target: name for name, target in spec.credential_renames.items()}
    name = public.get(dataclass_field, dataclass_field)
    if name in spec.credential_fields:
        return f"credentials.{name}"
    if dataclass_field in spec.tuning_fields:
        return f"advanced.{dataclass_field}"
    return f"config.{dataclass_field}"


def _connection_id(source_key: str) -> str:
    """The key itself when it is a valid ``connection_id``, else a slug of it."""
    return (
        source_key
        if CONNECTION_ID_PATTERN.fullmatch(source_key)
        else slugify_connection_id(source_key)
    )


def _invalid(
    source_key: str, project_id: str, reason: str, *, connection_id: str | None = None
) -> ImportChange:
    """A change that cannot be applied, with why."""
    return ImportChange(
        "invalid",
        source_key,
        project_id,
        connection_id or _connection_id(source_key),
        reason=reason,
    )


def _same_content(existing: Connection, desired: Connection) -> bool:
    """True when nothing an import sets differs; secret status stamps are ignored."""
    return (
        existing.source_type == desired.source_type
        and existing.name == desired.name
        and _secret_free(existing.credentials) == _secret_free(desired.credentials)
        and existing.config == desired.config
        and existing.tuning == desired.tuning
        and existing.status == desired.status
    )


def _secret_free(credentials: Mapping[str, Any]) -> dict[str, Any]:
    """Credentials with each secret reduced to its reference."""
    return {
        key: stored.ref.value if (stored := StoredSecret.from_json(value)) is not None else value
        for key, value in credentials.items()
    }


def _exported_credentials(credentials: Mapping[str, Any]) -> dict[str, Any]:
    """Secrets as ``{secret_ref}`` when external, as their status when stored."""
    exported: dict[str, Any] = {}
    for key, value in credentials.items():
        stored = StoredSecret.from_json(value)
        if stored is None:
            exported[key] = value
        elif stored.ref.is_stored:
            exported[key] = stored.status()
        else:
            exported[key] = {REFERENCE_KEY: stored.ref.value}
    return exported
