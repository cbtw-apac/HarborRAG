"""GitHub connector public API."""

from .config import GitHubRepositoryConfig
from .connector import GitHubConnector

__all__ = [
    "GitHubConnector",
    "GitHubRepositoryConfig",
]
