"""Application services behind the HTTP API and the CLI.

Runtime-layer use cases that turn a transport's arguments into one transport-neutral
`AppResponse`. Transports (HTTP, CLI) live in the app package and import this one; nothing
here imports them, and no Temporal object crosses this boundary.

Start from: get_app_service, select_app_service, BaseAppService, AppResponse.
"""

from typing import TYPE_CHECKING, Any

from .composition.selection import (
    get_app_service,
    runtime_app_service,
    select_app_service,
)
from .ports import BaseAppService
from .schemas import AppResponse

if TYPE_CHECKING:
    from .composition.service import AppService

__all__ = [
    "AppResponse",
    "AppService",
    "BaseAppService",
    "get_app_service",
    "runtime_app_service",
    "select_app_service",
]


def __getattr__(name: str) -> Any:
    """Keep the runtime client optional until a caller requests a concrete service."""
    if name == "AppService":
        from .composition.service import AppService

        return AppService
    raise AttributeError(name)
