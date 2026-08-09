from typing import TYPE_CHECKING, Any

from .ports import BaseAppService
from .schemas import AppResponse
from .selection import (
    development_app_service,
    get_app_service,
    runtime_app_service,
    select_app_service,
)

if TYPE_CHECKING:
    from .client import AppService
    from .mock import mock_app_service

__all__ = [
    "AppResponse",
    "AppService",
    "BaseAppService",
    "development_app_service",
    "get_app_service",
    "mock_app_service",
    "runtime_app_service",
    "select_app_service",
]


def __getattr__(name: str) -> Any:
    """Keep the runtime client optional until a caller requests a concrete service."""

    if name == "AppService":
        from .client import AppService

        return AppService
    if name == "mock_app_service":
        from .mock import mock_app_service

        return mock_app_service
    raise AttributeError(name)
