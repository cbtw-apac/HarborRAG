"""Process-group termination and error translation for the isolated runner.

Split from ``test_process_isolation_internals.py`` to stay under the repository
file-length gate. These helpers decide whether a runaway parser and its
descendants actually die, and how a sanitised child error envelope becomes a
typed HarborRAG exception.
"""

from __future__ import annotations

import os

import pytest

from harborrag_runtime.temporal.process_isolation import (
    IsolatedProcessRunner,
    ProcessLimits,
    _isolated_error,
    _kill_process_group,
)


class FakeProcess:
    """Duck-typed BaseProcess for the termination helper."""

    def __init__(self, *, pid: int | None, alive: bool = True) -> None:
        self.pid = pid
        self._alive = alive
        self.killed = False
        self.joined = False

    def is_alive(self) -> bool:
        return self._alive

    def kill(self) -> None:
        self.killed = True
        self._alive = False

    def join(self) -> None:
        self.joined = True


def _echo(value: str) -> str:
    return value


# --------------------------------------------------------------------------
# Process-group termination
# --------------------------------------------------------------------------


def test_kill_process_group_signals_the_whole_group(monkeypatch: pytest.MonkeyPatch) -> None:
    signalled: list[tuple[int, int]] = []
    monkeypatch.setattr(os, "name", "posix")
    monkeypatch.setattr(os, "killpg", lambda pid, sig: signalled.append((pid, sig)))
    process = FakeProcess(pid=4321)

    _kill_process_group(process)

    assert signalled[0][0] == 4321
    assert process.joined is True


def test_kill_process_group_falls_back_when_the_group_is_gone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reaped leader must still get a direct kill rather than an exception."""

    def _missing(pid: int, sig: int) -> None:
        raise ProcessLookupError

    monkeypatch.setattr(os, "name", "posix")
    monkeypatch.setattr(os, "killpg", _missing)
    process = FakeProcess(pid=4321, alive=True)

    _kill_process_group(process)

    assert process.killed is True
    assert process.joined is True


def test_kill_process_group_tolerates_an_unstarted_process() -> None:
    process = FakeProcess(pid=None, alive=True)

    _kill_process_group(process)

    assert process.killed is True
    assert process.joined is True


def test_kill_process_group_off_posix_kills_directly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(os, "name", "nt")
    process = FakeProcess(pid=4321, alive=True)

    _kill_process_group(process)

    assert process.killed is True


# --------------------------------------------------------------------------
# Error translation
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("envelope", "expected_type", "expected_message"),
    [
        (
            {"module": "harborrag_adapters.parsers.errors", "type": "UnsupportedFormatError"},
            "UnsupportedFormatError",
            "unsupported format in isolated parser",
        ),
        (
            {"module": "harborrag_adapters.parsers.errors", "type": "EncryptedPdfError"},
            "EncryptedPdfError",
            "encrypted PDF rejected by isolated parser",
        ),
        (
            {
                "module": "harborrag_adapters.parsers.pdf.engines.example",
                "type": "SomeParserError",
            },
            "ParseError",
            "SomeParserError in isolated parser",
        ),
        (
            {"module": "builtins", "type": "ValueError"},
            "RuntimeError",
            "ValueError in isolated document worker",
        ),
        ({}, "RuntimeError", "Exception in isolated document worker"),
    ],
)
def test_isolated_error_maps_envelopes_to_typed_errors(
    envelope: dict[str, str],
    expected_type: str,
    expected_message: str,
) -> None:
    error = _isolated_error(envelope)

    assert type(error).__name__ == expected_type
    assert str(error) == expected_message


# --------------------------------------------------------------------------
# Runner argument validation
# --------------------------------------------------------------------------


def test_runner_rejects_non_positive_concurrency() -> None:
    with pytest.raises(ValueError, match="concurrency must be positive"):
        IsolatedProcessRunner(max_concurrency=0)


@pytest.mark.asyncio
async def test_runner_rejects_a_non_positive_heartbeat_interval() -> None:
    runner = IsolatedProcessRunner(max_concurrency=1)

    with pytest.raises(ValueError, match="heartbeat interval must be positive"):
        await runner.run(_echo, "x", heartbeat=lambda: None, heartbeat_interval_seconds=0)


@pytest.mark.asyncio
async def test_runner_rejects_an_oversized_payload() -> None:
    runner = IsolatedProcessRunner(
        limits=ProcessLimits(max_payload_bytes=1),
        max_concurrency=1,
    )

    with pytest.raises(ValueError, match="input exceeds the configured byte limit"):
        await runner.run(
            _echo,
            "x" * 4096,
            heartbeat=lambda: None,
            heartbeat_interval_seconds=0.05,
        )
