"""Public chat completion and session API."""

from .routes import router
from .sessions import session_router

router.include_router(session_router("chat"))

__all__ = ["router"]
