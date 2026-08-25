from importlib.metadata import PackageNotFoundError, version

from harborrag_app.workflow_control import AppResponse, BaseAppService

try:
    __version__ = version("harborrag-app")
except PackageNotFoundError:  # pragma: no cover - source checkout without installation
    __version__ = "0+unknown"

__all__ = [
    "AppResponse",
    "BaseAppService",
    "__version__",
]
