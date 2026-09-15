"""Lazy Temporal imports with one public, secret-safe installation error."""

from importlib import import_module

from harborrag_runtime.errors import MissingOptionalDependencyError

TEMPORAL_EXTRA_HINT = 'Temporal commands require the client: pip install "harborrag[temporal]"'


def load_temporal_attribute(module_name: str, attribute: str) -> object:
    """Load one Temporal-backed collaborator without burdening direct-mode imports."""

    try:
        module = import_module(module_name)
    except ModuleNotFoundError as error:
        if (error.name or "").split(".")[0] == "temporalio":
            raise MissingOptionalDependencyError(TEMPORAL_EXTRA_HINT) from error
        raise
    return getattr(module, attribute)


__all__ = ["TEMPORAL_EXTRA_HINT", "load_temporal_attribute"]
