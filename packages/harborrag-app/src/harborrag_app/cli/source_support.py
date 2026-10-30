"""Connection references, body files and responses of the ``harborrag source`` commands."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Self

import yaml
from pydantic_core import to_jsonable_python

from harborrag_app.cli.runner import ResponseCall, failure
from harborrag_app.workflow_control import AppResponse, BaseAppService
from harborrag_app.workflow_control.control_plane.connections import (
    ConnectionKey,
    ConnectionService,
    ConnectionWrite,
)
from harborrag_core.contracts.errors import (
    HarborCapabilityError,
    HarborConfigurationError,
    HarborError,
    HarborValidationError,
)

BODY_FIELDS = frozenset({"source_type", "name", "credentials", "config", "advanced"})

type ConnectionCall = Callable[[ConnectionService], Awaitable[Mapping[str, Any]]]


@dataclass(frozen=True, slots=True)
class SourceRef:
    """A connection named as ``PROJECT/CONNECTION`` on the command line."""

    project_id: str
    connection_id: str

    @classmethod
    def parse(cls, value: str) -> SourceRef:
        """Split ``value`` into its project and connection ids.

        Raises:
            ValueError: ``value`` is not ``PROJECT/CONNECTION``.
        """
        project_id, separator, connection_id = value.strip().partition("/")
        if not separator or not project_id or not connection_id or "/" in connection_id:
            raise ValueError(
                f"expected PROJECT/CONNECTION, such as default/jira-cpm, got {value!r}"
            )
        return cls(project_id, connection_id)

    def key(self, tenant_id: str) -> ConnectionKey:
        """Return the service key of this connection in ``tenant_id``."""
        return ConnectionKey(tenant_id, self.project_id, self.connection_id)


class ConnectionWriteBuilder:
    """Build a ``ConnectionWrite`` from a body file in the shape of the connection ``PUT``.

    The file holds ``source_type``, ``name``, ``credentials``, ``config`` and
    ``advanced``, as a Swagger UI template does. Secrets come from the file as a
    value or ``{secret_ref: env:NAME}``, never from a command-line flag.
    """

    def __init__(self) -> None:
        self._document: dict[str, Any] = {}

    def from_file(self, path: Path) -> Self:
        """Read a YAML or JSON body file.

        Raises:
            ValueError: The file cannot be read or is not a mapping.
        """
        try:
            loaded = yaml.safe_load(path.expanduser().read_text(encoding="utf-8"))
        except OSError as error:
            raise ValueError(f"cannot read {path}: {error.strerror}") from None
        except yaml.YAMLError:
            raise ValueError(f"{path} is not valid YAML or JSON") from None
        if not isinstance(loaded, Mapping):
            raise ValueError(f"{path} must hold a mapping with source_type, credentials and config")
        return self.from_mapping(loaded)

    def from_mapping(self, document: Mapping[str, Any]) -> Self:
        """Use ``document`` as the body.

        Raises:
            ValueError: It has fields the connection ``PUT`` does not take.
        """
        unknown = sorted(set(document) - BODY_FIELDS)
        if unknown:
            allowed = ", ".join(sorted(BODY_FIELDS))
            raise ValueError(f"unknown field {', '.join(unknown)}; a body takes {allowed}")
        self._document = dict(document)
        return self

    def build(self) -> ConnectionWrite:
        """Return the write.

        Raises:
            ValueError: ``source_type`` is missing or a part has the wrong shape.
        """
        source_type = self._document.get("source_type")
        if not isinstance(source_type, str) or not source_type:
            raise ValueError("the body needs a source_type, such as jira")
        name = self._document.get("name")
        if name is not None and not isinstance(name, str):
            raise ValueError("name must be text")
        advanced = self._document.get("advanced")
        return ConnectionWrite(
            source_type=source_type,
            credentials=_mapping(self._document, "credentials"),
            config=_mapping(self._document, "config"),
            name=name,
            tuning=None if advanced is None else _mapping(self._document, "advanced"),
        )


def connection_call(operation: ConnectionCall) -> ResponseCall:
    """Run ``operation`` on the app service's connections and answer with an ``AppResponse``.

    Dates in the result become ISO 8601 strings, as the API returns them.
    """

    async def call(service: BaseAppService) -> AppResponse:
        try:
            connections = _connection_service(service)
            return AppResponse(True, data=to_jsonable_python(dict(await operation(connections))))
        except Exception as error:  # noqa: BLE001 - the CLI's stable error boundary
            return source_failure(error)

    return call


def source_failure(error: Exception) -> AppResponse:
    """Describe a failed connection call; service messages are written for the caller.

    A validation error lists each failing field. Configuration errors and anything
    outside ``HarborError`` keep the CLI's generic handling, which never prints
    infrastructure detail.
    """
    if isinstance(error, HarborConfigurationError) or not isinstance(error, HarborError):
        return failure(error)
    data: dict[str, Any] = {"error_type": type(error).__name__}
    message = str(error) or type(error).__name__
    if isinstance(error, HarborValidationError):
        issues = _field_issues(error)
        data["details"] = issues
        if issues:
            message = "; ".join(f"{issue['field']}: {issue['issue']}" for issue in issues)
    return AppResponse(False, data=data, error=message)


def _connection_service(service: BaseAppService) -> ConnectionService:
    """Return the service's ``ConnectionAdministration.connection_service``.

    Raises:
        HarborCapabilityError: This app service does not manage connections.
    """
    connections: ConnectionService | None = getattr(service, "connection_service", None)
    if connections is None:
        raise HarborCapabilityError("this HarborRAG setup does not manage connections")
    return connections


def _field_issues(error: HarborValidationError) -> list[dict[str, str]]:
    """Return a validation error's ``[{field, issue}]`` list."""
    errors = error.details.get("errors")
    if not isinstance(errors, list):
        return []
    return [
        {"field": str(item.get("field", "")), "issue": str(item.get("issue", ""))}
        for item in errors
        if isinstance(item, Mapping)
    ]


def _mapping(document: Mapping[str, Any], name: str) -> dict[str, Any]:
    """Return ``document[name]`` as a mapping; missing means empty."""
    value = document.get(name)
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be a mapping")
    return dict(value)
