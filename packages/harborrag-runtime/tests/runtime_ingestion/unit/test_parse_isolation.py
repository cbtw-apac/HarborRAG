"""A parse runs in a child process that is killed, with its children, at its deadline.

Real child processes, started with ``spawn`` so each test begins clean. The
targets are standard-library callables, so nothing from this test module
has to be importable in the child.
"""

from __future__ import annotations

import multiprocessing
import operator
import os
import pickle
import struct
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from harborrag_core.domain.parser import ParsedDocument
from harborrag_core.domain.raw_document import RawDocument
from harborrag_core.ingestion import (
    DocumentParseCrashError,
    DocumentParseTimeoutError,
    ParserRejectedDocumentError,
    UnsupportedDocumentError,
)
from harborrag_runtime.ingestion import parse_isolation as module
from harborrag_runtime.ingestion.parse_isolation import IsolatedDocumentParser, run_with_deadline

_REPO_ROOT = Path(__file__).resolve().parents[5]


def _raw() -> RawDocument:
    return RawDocument(
        id="doc-1",
        source="notes.md",
        content="# Release policy\n\nShip on Tuesdays.",
        content_type="text/markdown",
        metadata={"filename": "notes.md"},
    )


def _dead(pid: int) -> bool:
    """Gone, or a zombie waiting to be reaped by init: either way not running."""

    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except FileNotFoundError:
        return True
    return stat.rsplit(")", 1)[1].split()[0] == "Z"


def test_a_fast_child_returns_its_result() -> None:
    assert run_with_deadline(operator.add, (2, 3), deadline_seconds=30, start_method="spawn") == 5


def test_the_childs_own_exception_crosses_back_unchanged() -> None:
    with pytest.raises(ValueError, match="invalid literal"):
        run_with_deadline(int, ("not a number",), deadline_seconds=30, start_method="spawn")


def test_a_child_that_dies_without_a_result_is_a_crash() -> None:
    with pytest.raises(DocumentParseCrashError, match="without a result"):
        run_with_deadline(os._exit, (3,), deadline_seconds=30, start_method="spawn")


def test_a_hanging_child_is_killed_at_the_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    reaped: list[Any] = []
    real_reap = module._reap

    def recording_reap(process: Any) -> None:
        reaped.append(process)
        real_reap(process)

    monkeypatch.setattr(module, "_reap", recording_reap)
    began = time.monotonic()

    with pytest.raises(DocumentParseTimeoutError, match="exceeded 1s"):
        run_with_deadline(time.sleep, (120,), deadline_seconds=1.0, start_method="spawn")

    assert time.monotonic() - began < 15
    assert len(reaped) == 1 and not reaped[0].is_alive()


def test_the_deadline_also_kills_what_the_parse_started(tmp_path: Path) -> None:
    pidfile = tmp_path / "grandchild.pid"
    command = ["sh", "-c", f"echo $$ > {pidfile}; exec sleep 120"]

    with pytest.raises(DocumentParseTimeoutError):
        run_with_deadline(subprocess.run, (command,), deadline_seconds=5.0, start_method="spawn")

    grandchild = int(pidfile.read_text().strip())
    for _ in range(50):
        if _dead(grandchild):
            break
        time.sleep(0.1)
    assert _dead(grandchild)


def test_the_configured_catalog_parses_in_a_forkserver_child() -> None:
    parser = IsolatedDocumentParser.from_catalog(
        _REPO_ROOT / "config" / "parsers.yaml", deadline_seconds=60, max_concurrency=1
    )

    parsed = parser.parse(_raw())

    assert isinstance(parsed, ParsedDocument)
    assert "Ship on Tuesdays." in parsed.content


def test_a_target_that_returns_something_else_is_a_crash() -> None:
    parser = IsolatedDocumentParser(
        str, deadline_seconds=30, max_concurrency=1, start_method="spawn"
    )

    with pytest.raises(DocumentParseCrashError, match="something other than a parsed document"):
        parser.parse(_raw())


def test_parses_beyond_the_concurrency_bound_wait_for_a_slot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered = threading.Semaphore(0)
    release = threading.Event()
    active: list[int] = []
    peak: list[int] = []

    def fake_run(target: Any, args: Any, *, deadline_seconds: float, start_method: str) -> Any:
        active.append(1)
        peak.append(len(active))
        entered.release()
        release.wait(5)
        active.pop()
        return ParsedDocument(content="ok", parser_name="fake")

    monkeypatch.setattr(module, "run_with_deadline", fake_run)
    parser = IsolatedDocumentParser(str, deadline_seconds=1, max_concurrency=1)
    workers = [threading.Thread(target=parser.parse, args=(_raw(),)) for _ in range(2)]
    for worker in workers:
        worker.start()

    assert entered.acquire(timeout=5)
    assert not entered.acquire(timeout=0.2)  # the second parse is waiting for the slot
    release.set()
    for worker in workers:
        worker.join(5)
    assert max(peak) == 1


@pytest.mark.parametrize(
    ("deadline", "concurrency", "message"),
    [(0, 1, "deadline"), (float("nan"), 1, "deadline"), (1, 0, "concurrency")],
)
def test_bounds_are_validated(deadline: float, concurrency: int, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        IsolatedDocumentParser(str, deadline_seconds=deadline, max_concurrency=concurrency)


def test_an_oversized_result_is_a_crash_not_a_parent_allocation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(module, "_MAX_OUTCOME_BYTES", 1024)

    with pytest.raises(DocumentParseCrashError, match="too large"):
        run_with_deadline(operator.mul, ("x", 64 * 1024), deadline_seconds=30, start_method="spawn")


@pytest.mark.parametrize(
    "failure",
    [DocumentParseTimeoutError("stopped"), DocumentParseCrashError("died"), KeyError("x")],
)
def test_a_failed_parse_gives_its_slot_back(
    monkeypatch: pytest.MonkeyPatch, failure: BaseException
) -> None:
    def failing_run(target: Any, args: Any, *, deadline_seconds: float, start_method: str) -> Any:
        raise failure

    monkeypatch.setattr(module, "run_with_deadline", failing_run)
    parser = IsolatedDocumentParser(str, deadline_seconds=1, max_concurrency=1)

    with pytest.raises(type(failure)):
        parser.parse(_raw())

    assert parser._slots.acquire(blocking=False)


def _wait_dead(pid: int) -> bool:
    for _ in range(50):
        if _dead(pid):
            return True
        time.sleep(0.1)
    return _dead(pid)


def test_a_crashed_child_does_not_leave_what_it_started_running(tmp_path: Path) -> None:
    pidfile = tmp_path / "grandchild.pid"
    # The grandchild shell records a background sleep, then SIGKILLs the
    # Python child ($PPID) so it dies without a result while the sleep lives on.
    script = f"sleep 120 & echo $! > {pidfile}; sleep 0.3; kill -9 $PPID"

    with pytest.raises(DocumentParseCrashError, match="without a result"):
        run_with_deadline(
            subprocess.run, (["sh", "-c", script],), deadline_seconds=30, start_method="spawn"
        )

    assert _wait_dead(int(pidfile.read_text().strip()))


def test_a_forkserver_child_that_dies_without_a_result_is_a_crash() -> None:
    with pytest.raises(DocumentParseCrashError, match="without a result"):
        run_with_deadline(os._exit, (3,), deadline_seconds=30, start_method="forkserver")


def test_a_real_parser_rejection_keeps_its_classification() -> None:
    parser = IsolatedDocumentParser.from_catalog(
        _REPO_ROOT / "config" / "parsers.yaml", deadline_seconds=60, max_concurrency=1
    )
    garbage_pdf = RawDocument(
        id="doc-2",
        source="broken.pdf",
        content=b"%PDF-1.4 garbage",
        content_type="application/pdf",
        metadata={"filename": "broken.pdf"},
    )

    with pytest.raises(ParserRejectedDocumentError) as caught:
        parser.parse(garbage_pdf)

    assert not isinstance(caught.value, (DocumentParseCrashError, DocumentParseTimeoutError))


def test_an_unpicklable_result_is_a_crash() -> None:
    with pytest.raises(DocumentParseCrashError, match="could not be returned"):
        run_with_deadline(threading.Lock, (), deadline_seconds=30, start_method="spawn")


class _KeywordOnlyRejection(ParserRejectedDocumentError):
    def __init__(self, *, page_count: int) -> None:
        super().__init__(f"{page_count} pages")


class _KeywordOnlyUnsupported(UnsupportedDocumentError):
    def __init__(self, *, kind: str) -> None:
        super().__init__(kind)


class _KeywordOnlyFailure(Exception):
    def __init__(self, *, detail: str) -> None:
        super().__init__(detail)


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (_KeywordOnlyRejection(page_count=3), ParserRejectedDocumentError),
        (_KeywordOnlyUnsupported(kind="secret.bin"), UnsupportedDocumentError),
        (_KeywordOnlyFailure(detail="secret text"), DocumentParseCrashError),
    ],
)
def test_an_error_that_cannot_round_trip_is_replaced_with_its_classification(
    error: BaseException, expected: type[BaseException]
) -> None:
    kind, stand_in = pickle.loads(module._encode_outcome(("error", error)))

    assert kind == "error"
    assert type(stand_in) is expected
    assert type(error).__name__ in str(stand_in)
    assert "secret" not in str(stand_in) and "3 pages" not in str(stand_in)


def _feed(payload: bytes) -> Any:
    receiver, sender = multiprocessing.Pipe(duplex=False)
    os.write(sender.fileno(), payload)
    sender.close()
    try:
        return module._await_outcome(None, receiver, 5.0)
    finally:
        receiver.close()


def test_a_child_that_dies_partway_through_its_result_is_a_crash() -> None:
    with pytest.raises(DocumentParseCrashError, match="too large or unreadable"):
        _feed(struct.pack("!i", 1000) + b"partial")


def test_a_result_that_does_not_unpickle_is_a_crash() -> None:
    body = b"not a pickle"
    with pytest.raises(DocumentParseCrashError, match="could not be decoded"):
        _feed(struct.pack("!i", len(body)) + body)
