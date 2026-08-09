"""Runaway-loop protection for the bounded agent loop.

``AgentRunOptions.max_steps`` alone does not stop a pathological run: a model
can spend its entire step budget calling the same tool with the same
arguments, or one step can hang indefinitely on a slow tool. ``ExecutionGuard``
adds the two checks a step-count bound cannot express: a wall-clock deadline
and repeated-identical-tool-call detection.
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from json import dumps

from harborrag_core.ports.agent_runs import AgentToolExecution


def digest_arguments(arguments: dict[str, object]) -> str:
    """Stable hash of tool-call arguments, used as a repeat-detection key.

    Only the digest is ever persisted or compared -- never the raw
    arguments -- so this is safe to store in a public execution trace.
    """

    canonical = dumps(arguments, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(slots=True)
class ExecutionGuard:
    """Bound wall-clock time and repeated tool calls within one run attempt.

    A fresh guard is armed for each call to ``run()`` or ``resume()`` --
    ``timeout_seconds`` is a budget for *this* execution attempt, not the
    run's total lifetime, since a resumed run may follow an arbitrarily long
    gap after a crash.
    """

    timeout_seconds: float | None = None
    max_repeated_tool_calls: int = 2
    _deadline: float | None = field(default=None, init=False, repr=False)
    _call_counts: dict[tuple[str, str], int] = field(
        default_factory=dict,
        init=False,
        repr=False,
    )

    def __post_init__(self) -> None:
        if self.timeout_seconds is not None and self.timeout_seconds <= 0:
            raise ValueError("agent timeout_seconds must be positive")
        if self.max_repeated_tool_calls < 1:
            raise ValueError("agent max_repeated_tool_calls must be at least 1")

    def start(self) -> None:
        """Arm the deadline. Call exactly once at the start of an execution attempt."""

        self._deadline = (
            None if self.timeout_seconds is None else time.monotonic() + self.timeout_seconds
        )

    def remaining_seconds(self) -> float | None:
        """Seconds left before the deadline, or ``None`` when unbounded."""

        if self._deadline is None:
            return None
        return max(0.0, self._deadline - time.monotonic())

    def timed_out(self) -> bool:
        remaining = self.remaining_seconds()
        return remaining is not None and remaining <= 0

    def observe_tool_call(self, tool: str, arguments_digest: str) -> bool:
        """Record one tool call; return ``True`` once its repeat limit is exceeded."""

        key = (tool, arguments_digest)
        count = self._call_counts.get(key, 0) + 1
        self._call_counts[key] = count
        return count > self.max_repeated_tool_calls

    def replay(self, executions: Iterable[AgentToolExecution]) -> None:
        """Rebuild repeated-call state from a checkpoint's executions on resume."""

        for execution in executions:
            key = (execution.tool, execution.arguments_digest)
            self._call_counts[key] = self._call_counts.get(key, 0) + 1


__all__ = ["ExecutionGuard", "digest_arguments"]
