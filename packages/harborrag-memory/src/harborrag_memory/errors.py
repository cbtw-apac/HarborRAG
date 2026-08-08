"""Memory-specific exceptions."""

from __future__ import annotations


class MemoryError(Exception):
    """Base class for memory orchestration errors."""


class MemoryConfigurationError(ValueError, MemoryError):
    """Raised when a caller asks for a memory capability that is not configured."""


class MemoryScopeError(ValueError, MemoryError):
    """Raised when a caller provides an owner that does not satisfy a scope."""