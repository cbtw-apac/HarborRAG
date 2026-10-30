"""What a transport needs from the app service to manage connections and projects."""

from __future__ import annotations

from typing import Protocol

from .connections import ConnectionService
from .projects import ProjectService


class ConnectionAdministration(Protocol):
    """The part of the app service the HTTP routes and the CLI ``source`` commands use."""

    @property
    def connection_service(self) -> ConnectionService: ...

    @property
    def project_service(self) -> ProjectService: ...
