from __future__ import annotations

from dataclasses import dataclass
from typing import Any


class Status:
    SUCCESS = "success"
    FAILURE = "failure"
    PENDING = "pending"
    IN_PROGRESS = "in_progress"


@dataclass
class Input:
    """Input schemas"""


@dataclass
class InputGet(Input):
    """Input schema for a GET request"""

    id: str


@dataclass
class FetchResult:
    """Fetch result schemas"""

    status: Status
    result: Any
    message: str | None = None
