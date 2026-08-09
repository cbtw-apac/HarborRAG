"""Memory-specific exceptions."""

from __future__ import annotations

from harborrag_core.contracts.errors import (
    HarborConfigurationError,
    HarborError,
    HarborSecurityError,
)


class MemoryError(HarborError):
    """Base class for memory orchestration errors."""


class MemoryConfigurationError(HarborConfigurationError, MemoryError):
    """Raised when a caller asks for a memory capability that is not configured."""


class MemoryScopeError(HarborSecurityError, MemoryError):
    """Raised when a caller provides an owner that does not satisfy a scope."""
