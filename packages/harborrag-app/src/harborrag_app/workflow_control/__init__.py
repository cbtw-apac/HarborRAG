from typing import Any

from .ports import BaseAppService
from .schemas import AppResponse
from .selection import (
    development_app_service,
    get_app_service,
    runtime_app_service,
    select_app_service,
)

__all__ = [
    "AppResponse",
    "AppService",
    "BaseAppService",
    "development_app_service",
    "get_app_service",
    "runtime_app_service",
    "select_app_service",
]


def __getattr__(name: str) -> Any:
    """Keep Temporal optional until a caller explicitly requests AppService."""

    if name == "AppService":
        from .client import AppService

        return AppService
    raise AttributeError(name)
