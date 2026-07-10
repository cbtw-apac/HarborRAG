from __future__ import annotations

from abc import ABC
from typing import Any


class Status:
    SUCCESS = "success"
    FAILURE = "failure"
    PENDING = "pending"
    IN_PROGRESS = "in_progress"

class Input:
    """Input schemas"""
    pass

class InputGet(Input):
    """Input schema for a GET request"""
    id: str

class FetchResult(ABC):
    """Fetch result schemas"""
    status: Status
    result: Any
    message: str | None = None
