"""The beta access file: which connections each principal may read, per tenant and project.

An operator writes ``config/access.yaml`` to grant beta users datasources. A datasource is
named by its connection id, the key it has in ``config/connectors.yaml``::

    version: 1
    tenants:
      acme:
        projects:
          beta-all:
            datasources: [confluence-eng, jira-cpm]
            members:
              - principal: "6d2f...e91a"
                label: alice@acme.com         # optional, for people only, never matched
                datasources: [confluence-eng]  # optional narrowing within the project

A member is entitled to the whole of its project's datasources, or only to the ones it lists.
The rule itself is :func:`harborrag_core.security.entitlements.entitled_scopes`, applied here
to connection ids; resolving them to source scope ids is the retrieval service's job.

Neither a member's ``label`` nor its ``principal`` appears in any error message, repr or log
line: errors name a member by its position in the project, and the watcher logs only the
exception type and the file's path.
"""

from __future__ import annotations

import logging
import os
import threading
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType

import yaml

from harborrag_core.security.directory import ProjectMember
from harborrag_core.security.entitlements import entitled_scopes
from harborrag_runtime.config.connectors.loader import load_connector_catalog
from harborrag_runtime.config.connectors.schemas import ConnectorCatalog
from harborrag_runtime.config.errors import ConnectorConfigurationError, ConnectorNotFoundError
from harborrag_runtime.config.loading import (
    read_yaml_file,
    reject_unknown_keys,
    require_nonblank_string,
    require_optional_nonblank_string,
    require_schema_version,
    require_string_mapping,
)

ACCESS_FILE_VERSION = 1
# Matches the longest ``AccessContext.principal_id``.
PRINCIPAL_MAX_LENGTH = 255

logger = logging.getLogger("harborrag.runtime.config.access_file")

_NAME = "Access configuration"
_ROOT_KEYS = frozenset({"version", "tenants"})
_TENANT_KEYS = frozenset({"projects"})
_PROJECT_KEYS = frozenset({"datasources", "members"})
_MEMBER_KEYS = frozenset({"principal", "label", "datasources"})


@dataclass(frozen=True, slots=True)
class AccessMember:
    """One principal of a project, optionally narrowed to some of its datasources.

    ``principal`` and ``label`` stay out of the repr, so logging an object never prints them.
    ``datasources`` of ``None`` means every datasource of the project.
    """

    principal: str = field(repr=False)
    label: str | None = field(default=None, repr=False)
    datasources: frozenset[str] | None = None


@dataclass(frozen=True, slots=True)
class AccessProject:
    """The connections a project grants, and the principals who hold it."""

    datasources: frozenset[str]
    members: tuple[AccessMember, ...]


@dataclass(frozen=True, slots=True)
class AccessFile:
    """A validated access file: tenant, then project, then the project's grant."""

    tenants: Mapping[str, Mapping[str, AccessProject]]

    def __post_init__(self) -> None:
        """Copy the maps into read-only views."""
        object.__setattr__(
            self,
            "tenants",
            MappingProxyType(
                {
                    tenant: MappingProxyType(dict(projects))
                    for tenant, projects in self.tenants.items()
                }
            ),
        )

    def entitled_connections(self, tenant_id: str, principal_id: str) -> frozenset[str] | None:
        """The connection ids ``principal_id`` may read in ``tenant_id``.

        ``None`` when the tenant is not in the file at all, which is different from a tenant
        the principal has no grant in (an empty set).
        """

        projects = self.tenants.get(tenant_id)
        if projects is None:
            return None
        memberships = [
            ProjectMember(project_id, member.principal, "reader", member.datasources)
            for project_id, project in projects.items()
            for member in project.members
            if member.principal == principal_id
        ]
        return entitled_scopes(
            memberships,
            {project_id: project.datasources for project_id, project in projects.items()},
            principal_id,
        )


def load_access_file(path: str | Path, *, catalog: ConnectorCatalog) -> AccessFile:
    """Load and validate an access file against the connector catalog.

    Every connection a project lists must be one ``catalog`` allows that project's tenant.

    Raises:
        ConnectorConfigurationError: If the file is missing, unreadable or not valid. The
            message names the key or connection id at fault, never a member's label or
            principal.
    """
    root = _mapping(_read_root(path), "access configuration root")
    require_schema_version(
        root.get("version"),
        expected=ACCESS_FILE_VERSION,
        label=_NAME,
        error_type=ConnectorConfigurationError,
    )
    reject_unknown_keys(
        root,
        _ROOT_KEYS,
        label="access configuration root",
        error_type=ConnectorConfigurationError,
    )
    raw_tenants = _mapping(root.get("tenants"), "tenants")
    return AccessFile(
        {
            _text(tenant_id, "tenants key"): _parse_tenant(tenant_id, raw_tenant, catalog)
            for tenant_id, raw_tenant in raw_tenants.items()
        }
    )


def _read_root(path: str | Path) -> object:
    """The file's decoded YAML, with a syntax error described without quoting the file."""

    try:
        _, raw = read_yaml_file(path, label=_NAME, error_type=ConnectorConfigurationError)
    except ConnectorConfigurationError as exc:
        cause = exc.__cause__
        if not isinstance(cause, yaml.YAMLError):
            raise
        source = Path(path).expanduser().resolve()
        # PyYAML's own message quotes the offending source line, which may be a label.
        raise ConnectorConfigurationError(
            f"Could not read {_NAME.lower()} {source}: {_yaml_problem(cause)}"
        ) from None
    return raw


def _yaml_problem(error: yaml.YAMLError) -> str:
    """What PyYAML found and where, without the text of the lines around it."""

    if not isinstance(error, yaml.MarkedYAMLError):
        return type(error).__name__
    problem = error.problem or type(error).__name__
    mark = error.problem_mark
    if mark is None:
        return problem
    return f"{problem} (line {mark.line + 1}, column {mark.column + 1})"


def _mapping(value: object, label: str) -> Mapping[str, object]:
    return require_string_mapping(value, label=label, error_type=ConnectorConfigurationError)


def _text(value: object, label: str) -> str:
    return require_nonblank_string(value, label=label, error_type=ConnectorConfigurationError)


def _only_keys(value: Mapping[str, object], allowed: frozenset[str], label: str) -> None:
    reject_unknown_keys(value, allowed, label=label, error_type=ConnectorConfigurationError)


def _parse_tenant(
    tenant_id: str, raw: object, catalog: ConnectorCatalog
) -> Mapping[str, AccessProject]:
    label = f"tenants.{tenant_id}"
    tenant = _mapping(raw, label)
    _only_keys(tenant, _TENANT_KEYS, label)
    raw_projects = _mapping(tenant.get("projects"), f"{label}.projects")
    return {
        _text(project_id, f"{label}.projects key"): _parse_project(
            tenant_id, project_id, raw_project, catalog
        )
        for project_id, raw_project in raw_projects.items()
    }


def _parse_project(
    tenant_id: str, project_id: str, raw: object, catalog: ConnectorCatalog
) -> AccessProject:
    label = f"tenants.{tenant_id}.projects.{project_id}"
    project = _mapping(raw, label)
    _only_keys(project, _PROJECT_KEYS, label)
    datasources = _connection_ids(project.get("datasources"), f"{label}.datasources")
    for connection_id in sorted(datasources):
        _require_connection(connection_id, tenant_id, catalog, label)
    members = _parse_members(project.get("members"), label, datasources)
    return AccessProject(datasources=datasources, members=members)


def _require_connection(
    connection_id: str, tenant_id: str, catalog: ConnectorCatalog, label: str
) -> None:
    """Refuse a connection the catalog does not offer this tenant, as it refuses a tenant."""

    try:
        catalog.get_for_tenant(connection_id, tenant_id)
    except ConnectorNotFoundError as exc:
        raise ConnectorConfigurationError(
            f"{label} lists connection {connection_id!r}, which is not a configured "
            f"connection of tenant {tenant_id!r}"
        ) from exc


def _parse_members(
    raw: object, project_label: str, project_datasources: frozenset[str]
) -> tuple[AccessMember, ...]:
    label = f"{project_label}.members"
    if not isinstance(raw, list):
        raise ConnectorConfigurationError(f"{label} must be a list")
    members: list[AccessMember] = []
    first_listed: dict[str, int] = {}
    for index, raw_member in enumerate(raw):
        member = _parse_member(raw_member, f"{label}[{index}]", project_datasources)
        if member.principal in first_listed:
            raise ConnectorConfigurationError(
                f"{label}[{index}] repeats the principal of members[{first_listed[member.principal]}]"
                "; a principal may not appear more than once in a project"
            )
        first_listed[member.principal] = index
        members.append(member)
    return tuple(members)


def _parse_member(raw: object, label: str, project_datasources: frozenset[str]) -> AccessMember:
    member = _mapping(raw, label)
    _only_keys(member, _MEMBER_KEYS, label)
    principal = _text(member.get("principal"), f"{label}.principal")
    if len(principal) > PRINCIPAL_MAX_LENGTH:
        raise ConnectorConfigurationError(
            f"{label}.principal must be at most {PRINCIPAL_MAX_LENGTH} characters"
        )
    person = require_optional_nonblank_string(
        member.get("label"), label=f"{label}.label", error_type=ConnectorConfigurationError
    )
    # An empty `datasources:` is refused rather than read as "no narrowing", which would
    # widen the member to the whole project.
    narrowed = None
    if "datasources" in member:
        narrowed = _connection_ids(member["datasources"], f"{label}.datasources")
        outside = sorted(narrowed - project_datasources)
        if outside:
            raise ConnectorConfigurationError(
                f"{label}.datasources lists connection(s) outside its project: {', '.join(outside)}"
            )
    return AccessMember(principal=principal, label=person, datasources=narrowed)


def _connection_ids(value: object, label: str) -> frozenset[str]:
    """A YAML list of connection ids; a bare string would otherwise read as its characters."""

    if not isinstance(value, list):
        raise ConnectorConfigurationError(f"{label} must be a list of connection ids")
    ids = [_text(item, f"{label}[{index}]") for index, item in enumerate(value)]
    repeated = sorted(name for name, count in Counter(ids).items() if count > 1)
    if repeated:
        raise ConnectorConfigurationError(
            f"{label} lists connection(s) more than once: {', '.join(repeated)}"
        )
    return frozenset(ids)


type _Signature = tuple[int, int] | None


@dataclass(frozen=True, slots=True)
class _Observed:
    """What the file looked like at a stat, and what it loaded to."""

    signature: _Signature
    access: AccessFile | None


class AccessFileWatcher:
    """The access file as it stands now, reloaded when it changes.

    :meth:`current` stats the file on every call and reloads only when its
    ``(st_mtime_ns, st_size)`` differs from the last call's. While the file is missing or
    invalid it returns ``None``, which callers treat as "refuse everyone"; one warning per
    change records the exception type and the path, never the file's contents.

    Nothing is read until the first call. Several watchers over one file are independent,
    and each reloads on its own.
    """

    def __init__(self, path: Path, *, catalog_path: Path) -> None:
        # Neither path is resolved here: a config mount that swaps a symlink on update
        # (as Kubernetes does for a ConfigMap) must be followed afresh on each stat.
        self._path = Path(path).expanduser()
        self._catalog_path = Path(catalog_path).expanduser()
        self._lock = threading.Lock()
        self._observed: _Observed | None = None

    def current(self) -> AccessFile | None:
        """The loaded file, or ``None`` while it is missing or invalid."""

        signature = self._signature()
        observed = self._observed
        if observed is None or observed.signature != signature:
            with self._lock:
                observed = self._observed
                if observed is None or observed.signature != signature:
                    observed = _Observed(signature, self._load())
                    self._observed = observed
        return observed.access

    def listed_tenants(self, principal_id: str) -> frozenset[str]:
        """The tenants in which ``principal_id`` may read at least one connection."""

        access = self.current()
        if access is None:
            return frozenset()
        return frozenset(
            tenant_id
            for tenant_id in access.tenants
            if access.entitled_connections(tenant_id, principal_id)
        )

    def _signature(self) -> _Signature:
        try:
            stat = os.stat(self._path)
        except OSError:
            return None
        return (stat.st_mtime_ns, stat.st_size)

    def _load(self) -> AccessFile | None:
        try:
            catalog = load_connector_catalog(self._catalog_path)
            return load_access_file(self._path, catalog=catalog)
        except Exception as exc:  # noqa: BLE001 - any failure must refuse, never raise into a request
            logger.warning(
                "Access file %s (connector catalog %s) could not be loaded: %s; every "
                "principal is refused until it is fixed",
                self._path,
                self._catalog_path,
                type(exc).__name__,
            )
            return None


__all__ = [
    "ACCESS_FILE_VERSION",
    "AccessFile",
    "AccessFileWatcher",
    "AccessMember",
    "AccessProject",
    "PRINCIPAL_MAX_LENGTH",
    "load_access_file",
]
