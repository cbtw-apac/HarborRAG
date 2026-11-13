"""What a transport needs from the app service to manage connections and projects."""

from __future__ import annotations

from typing import Protocol

from ..ingestion.failures import FailuresService
from .connections import ConnectionService
from .projects import ProjectService
from .settings import SettingsService


class ConnectionAdministration(Protocol):
    """The part of the app service the HTTP routes and the CLI ``source`` commands use."""

    @property
    def connection_service(self) -> ConnectionService: ...

    @property
    def project_service(self) -> ProjectService: ...

    @property
    def settings_service(self) -> SettingsService: ...

    @property
    def failures_service(self) -> FailuresService: ...
