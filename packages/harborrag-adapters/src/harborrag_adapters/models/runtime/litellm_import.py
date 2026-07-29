"""Own the single seam through which this package imports the optional `litellm`.

LiteLLM ships in the `llm` extra, so every entry point that touches it has to
decide what a missing install means. Concentrating that decision here keeps the
install hint, the failure type, and the lazy-import placement in one place
instead of restating them at each provider call site.
"""

from __future__ import annotations

from types import ModuleType

from harborrag_core.models import errors as model_errors

_INSTALL_HINT = (
    "LiteLLM support requires the `llm` extra; "
    "install `harborrag-adapters[llm]` or `pip install litellm`."
)


def require_litellm[E: model_errors.HarborModelError](error_type: type[E]) -> ModuleType:
    """Import `litellm`, raising the caller's family error when it is absent.

    Callers pass their own configuration error type so a missing extra surfaces
    as a chat/embed/rerank failure the caller already handles, rather than a
    bare `ImportError` escaping the adapter boundary.
    """

    try:
        import litellm
    except ImportError as exc:
        raise error_type(_INSTALL_HINT) from exc
    return litellm


def optional_litellm() -> ModuleType | None:
    """Return `litellm`, or `None` when the optional extra is not installed.

    For callers that must degrade rather than fail — error classification still
    has to work when LiteLLM is absent, falling back to transport-level
    categories it can determine without the SDK's exception hierarchy.
    """

    try:
        import litellm
    except ImportError:
        return None
    return litellm
