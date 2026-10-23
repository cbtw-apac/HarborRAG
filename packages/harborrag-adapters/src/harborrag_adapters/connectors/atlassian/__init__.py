"""Shared Atlassian REST client and Cloud host helper for Jira and Confluence."""

from .client import AtlassianHttpConfig, AtlassianRestClient
from .query import is_cloud_hostname

__all__ = ["AtlassianHttpConfig", "AtlassianRestClient", "is_cloud_hostname"]
