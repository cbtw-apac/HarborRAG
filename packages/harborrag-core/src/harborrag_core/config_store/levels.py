"""Settings levels: where an override lives and the chain a value resolves through.

Pure value objects; knows nothing about storage or the registry.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from harborrag_core.contracts.errors import HarborValidationError
from harborrag_core.domain.connection import require_connection_id

LevelKind = Literal["tenant", "project", "connection"]

_LEVEL_FIELD: dict[str, object] = {"field": "level"}


def _malformed(reason: str) -> HarborValidationError:
    return HarborValidationError(f"level is malformed: {reason}", details=dict(_LEVEL_FIELD))


@dataclass(frozen=True, slots=True)
class SettingsLevel:
    """One level of the settings hierarchy.

    Args:
        kind: Level kind.
        project_id: Project of a project or connection level.
        connection_id: Connection of a connection level.
    """

    kind: LevelKind
    project_id: str | None = None
    connection_id: str | None = None

    @classmethod
    def parse(cls, text: str | None) -> SettingsLevel:
        """Parse ``tenant``, ``project:<project_id>`` or ``connection:<project_id>/<connection_id>``.

        Args:
            text: Level label; ``None`` means tenant level.

        Returns:
            The parsed level.

        Raises:
            HarborValidationError: The label is malformed (``field = "level"``).
        """
        if text is None or text == "tenant":
            return cls("tenant")
        kind, _, rest = text.partition(":")
        if kind == "project" and rest and "/" not in rest and rest.strip() == rest:
            return cls("project", project_id=rest)
        if kind == "connection":
            project_id, _, connection_id = rest.partition("/")
            if not project_id or not connection_id or project_id.strip() != project_id:
                raise _malformed("expected connection:<project_id>/<connection_id>")
            try:
                require_connection_id(connection_id)
            except ValueError as error:
                raise _malformed(str(error)) from error
            return cls("connection", project_id=project_id, connection_id=connection_id)
        raise _malformed(
            "expected tenant, project:<project_id> or connection:<project_id>/<connection_id>"
        )

    def __str__(self) -> str:
        if self.kind == "tenant":
            return "tenant"
        if self.kind == "project":
            return f"project:{self.project_id}"
        return f"connection:{self.project_id}/{self.connection_id}"

    def chain(self) -> tuple[SettingsLevel, ...]:
        """Return the levels from tenant down to this one, least specific first."""
        tenant = SettingsLevel("tenant")
        if self.kind == "tenant":
            return (tenant,)
        project = SettingsLevel("project", project_id=self.project_id)
        if self.kind == "project":
            return (tenant, project)
        return (tenant, project, self)
