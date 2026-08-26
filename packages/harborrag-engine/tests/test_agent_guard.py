"""Unit tests for ExecutionGuard: wall-clock timeout and repeated-call detection."""

from __future__ import annotations

import time

import pytest

from harborrag_core.ports.agent_runs import AgentToolExecution
from harborrag_engine.agent.guard import ExecutionGuard, digest_arguments


def test_guard_without_timeout_never_times_out() -> None:
    guard = ExecutionGuard()
    guard.start()
    assert guard.remaining_seconds() is None
    assert guard.timed_out() is False


def test_guard_times_out_after_budget_elapses() -> None:
    guard = ExecutionGuard(timeout_seconds=0.01)
    guard.start()
    assert guard.timed_out() is False
    time.sleep(0.02)
    assert guard.timed_out() is True
    assert guard.remaining_seconds() == 0.0


def test_guard_rejects_non_positive_timeout() -> None:
    with pytest.raises(ValueError, match="positive"):
        ExecutionGuard(timeout_seconds=0)


def test_guard_rejects_non_positive_repeat_limit() -> None:
    with pytest.raises(ValueError, match="at least 1"):
        ExecutionGuard(max_repeated_tool_calls=0)


def test_guard_trips_only_after_repeat_limit_exceeded() -> None:
    guard = ExecutionGuard(max_repeated_tool_calls=2)
    digest = digest_arguments({"query": "x"})
    assert guard.observe_tool_call("vector_search", digest) is False
    assert guard.observe_tool_call("vector_search", digest) is False
    assert guard.observe_tool_call("vector_search", digest) is True


def test_guard_tracks_distinct_arguments_independently() -> None:
    guard = ExecutionGuard(max_repeated_tool_calls=1)
    first = digest_arguments({"query": "a"})
    second = digest_arguments({"query": "b"})
    assert guard.observe_tool_call("vector_search", first) is False
    assert guard.observe_tool_call("vector_search", second) is False
    assert guard.observe_tool_call("vector_search", first) is True


def test_digest_arguments_is_stable_regardless_of_key_order() -> None:
    assert digest_arguments({"a": 1, "b": 2}) == digest_arguments({"b": 2, "a": 1})


def test_guard_replay_rebuilds_repeat_state_from_checkpoint_executions() -> None:
    digest = digest_arguments({"query": "x"})
    guard = ExecutionGuard(max_repeated_tool_calls=2)
    guard.replay(
        [
            AgentToolExecution(
                step=1, call_id="c1", tool="vector_search", ok=True, arguments_digest=digest
            ),
            AgentToolExecution(
                step=2, call_id="c2", tool="vector_search", ok=True, arguments_digest=digest
            ),
        ]
    )
    assert guard.observe_tool_call("vector_search", digest) is True
