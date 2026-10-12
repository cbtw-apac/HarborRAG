"""Killable, isolated document parsing.

A parse runs in a child process so that a hostile or pathological document
(a regex that backtracks for minutes, a native library that never returns) can
be stopped. ``asyncio.to_thread`` alone could not do that: a timed-out thread
keeps running, and every Temporal retry started another one until the worker
was wedged.

``IsolatedDocumentParser`` has the same blocking ``parse(raw)`` shape as the
in-process parser, so the document stages keep calling it through
``asyncio.to_thread``. Inside that worker thread it starts a child, waits up
to ``deadline_seconds`` of wall-clock time, and then kills the child's whole
process group (engines such as MinerU start their own subprocesses). The
thread therefore always returns within the deadline plus a short join.

The parser registry does not pickle -- it holds local closures -- which is
why the earlier ``temporal/process_isolation.py`` attempt never ran. The
child receives only a picklable target such as
``functools.partial(parse_with_catalog, "/abs/parsers.yaml")`` and rebuilds
the configured registry itself. The outcome is pickled on the child's side
before it is sent, so an unpicklable result becomes an error rather than a
silent hang.

That outcome is unpickled in the parent. It comes from our own child over a
private pipe and is as trusted as the parser code that produced it, which
already runs on hostile input, so unpickling it adds no new trust boundary.
Its size is still bounded before anything is allocated for it, so a parse
that yields an enormous result fails instead of exhausting the parent's
memory. The child's stdout and stderr are inherited, never read by the parent.

POSIX only (process groups, ``os.killpg``); workers and CI run on Linux.
"""

from __future__ import annotations

import contextlib
import functools
import logging
import math
import multiprocessing
import os
import pickle
import signal
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal, cast

from harborrag_core.domain.parser import ParsedDocument
from harborrag_core.domain.raw_document import RawDocument
from harborrag_core.ingestion import (
    DocumentParseCrashError,
    DocumentParseTimeoutError,
    ParserRejectedDocumentError,
    UnsupportedDocumentError,
)

logger = logging.getLogger("harborrag.runtime.ingestion.parse_isolation")

StartMethod = Literal["forkserver", "spawn"]
ParseTarget = Callable[[RawDocument], ParsedDocument]

_JOIN_SECONDS = 5.0
# Caps what one parse may allocate in the parent (per concurrent slot). Checked
# against the message length header before the payload is read, so an
# oversized result is never allocated.
_MAX_OUTCOME_BYTES = 256 * 1024 * 1024
# Imported once by the fork server, so each forked parse skips the parser
# imports instead of paying them per document. The catalog itself is still
# loaded per parse, in the child. Note that a forkserver child also re-imports
# the parent's ``__main__`` (as ``__mp_main__``) on every parse.
_FORKSERVER_PRELOAD = ["harborrag_runtime.config.parsers", "harborrag_adapters.parsers"]


def parse_with_catalog(parser_config_path: str, source: RawDocument) -> ParsedDocument:
    """Child-side target: build the configured parser registry and parse one document."""

    from harborrag_runtime.config.parsers import load_parser_catalog

    registry = load_parser_catalog(Path(parser_config_path)).build_harbor_parser()
    return registry.parse(source)


def _child_main(target: Callable[..., Any], args: tuple[Any, ...], sender: Any) -> None:
    """Child entry: own process group, run ``target``, send one pickled outcome."""

    os.setpgid(0, 0)  # so the parent can kill anything this parse starts, too
    try:
        outcome: tuple[str, Any] = ("done", target(*args))
    except BaseException as error:  # noqa: BLE001 - every failure crosses back to the parent
        outcome = ("error", error)
    sender.send_bytes(_encode_outcome(outcome))
    sender.close()


def _encode_outcome(outcome: tuple[str, Any]) -> bytes:
    """Pickle ``outcome``, or a stand-in if it would not survive the trip back.

    An exception can pickle yet fail to unpickle (a keyword-only ``__init__``),
    which would reach the parent as a bare ``TypeError``. So the round trip is
    checked here, and a failing outcome is replaced by one that keeps the
    original's classification but carries only its class name.
    """

    try:
        payload = pickle.dumps(outcome)
        pickle.loads(payload)
    except Exception:  # noqa: BLE001 - an unpicklable or unrebuildable result or exception
        return pickle.dumps(("error", _stand_in(outcome)))
    return payload


def _stand_in(outcome: tuple[str, Any]) -> BaseException:
    kind, value = outcome
    if kind == "error" and isinstance(value, ParserRejectedDocumentError):
        rejected = (
            UnsupportedDocumentError
            if isinstance(value, UnsupportedDocumentError)
            else ParserRejectedDocumentError
        )
        return rejected(f"parser rejected the document with {type(value).__name__}")
    returned = type(value).__name__
    return DocumentParseCrashError(f"parse outcome could not be returned: {returned}")


def run_with_deadline(
    target: Callable[..., Any],
    args: tuple[Any, ...],
    *,
    deadline_seconds: float,
    start_method: StartMethod = "forkserver",
) -> Any:
    """Run ``target(*args)`` in a child process; kill it if it outlives the deadline.

    Blocking: call it from a worker thread. The child's own exception is
    re-raised unchanged, so parser errors keep their classification.
    """

    context = _context(start_method)
    receiver, sender = context.Pipe(duplex=False)
    try:
        process = context.Process(
            target=_child_main, args=(target, args, sender), name="harborrag-parse"
        )
        try:
            process.start()
        finally:
            sender.close()  # the child now holds the only writer, so its death reads as EOF
        try:
            return _await_outcome(process, receiver, deadline_seconds)
        finally:
            _reap(process)
    finally:
        receiver.close()


def _await_outcome(process: Any, receiver: Any, deadline_seconds: float) -> Any:
    if not receiver.poll(deadline_seconds):
        logger.warning(
            "Document parse exceeded %gs; killing parse process pid=%s",
            deadline_seconds,
            process.pid,
        )
        _kill_group(process)
        raise DocumentParseTimeoutError(
            f"document parse exceeded {deadline_seconds:g}s and was stopped"
        )
    kind, value = _decode_outcome(_receive_outcome(receiver))
    if kind == "error":
        raise cast(BaseException, value)
    return value


def _receive_outcome(receiver: Any) -> bytes:
    try:
        return cast(bytes, receiver.recv_bytes(_MAX_OUTCOME_BYTES))
    except EOFError:
        raise DocumentParseCrashError("parse process exited without a result") from None
    except OSError:  # chiefly a length header over the bound; the payload is never read
        raise DocumentParseCrashError("parse process result was too large or unreadable") from None


def _decode_outcome(payload: bytes) -> tuple[str, Any]:
    try:
        kind, value = pickle.loads(payload)
    except Exception:  # noqa: BLE001 - a corrupt or unrebuildable outcome
        raise DocumentParseCrashError("parse process result could not be decoded") from None
    return cast(str, kind), value


def _kill_group(process: Any) -> None:
    """SIGKILL the child and everything it started.

    The group id is the child's pid. Under ``spawn`` the parent has not
    reaped the child yet, so the pid is still the child's. Under
    ``forkserver`` the server may already have reaped it; the group then
    lives on only while something the parse started is still in it, and
    Linux does not reuse a pid that is still a process group id. If the
    group is empty, the signal fails with ``ProcessLookupError``, which is
    ignored. Reuse is possible only in the narrow case where the group has
    emptied and an unrelated process has since taken the pid as its own
    group leader.
    """

    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(process.pid, signal.SIGKILL)


def _reap(process: Any) -> None:
    """Make sure the child is gone, then reap it; runs exactly once per child."""

    _kill_group(process)  # also when the child crashed: what it started may live on
    if process.is_alive():  # an exception in the parent, or a child stuck exiting
        process.kill()  # in case it had not yet moved into its own group
    process.join(_JOIN_SECONDS)
    if process.exitcode is None:
        logger.warning("Parse process pid=%s was not reaped within %gs", process.pid, _JOIN_SECONDS)


def _context(start_method: StartMethod) -> Any:
    context = multiprocessing.get_context(start_method)
    if start_method == "forkserver":
        # Effective only before the server first starts; harmless afterwards.
        cast(Any, context).set_forkserver_preload(_FORKSERVER_PRELOAD)
    return context


class IsolatedDocumentParser:
    """A ``DocumentParserPort`` that parses in a killable child, a bounded number at once.

    Waiting for a free slot does not count against the deadline. One instance
    serves every parsing path in a worker process (the parse activity, retries,
    connector-free reindex), so under load a parse can wait; the activity's
    heartbeat budget allows for one full slot wait (see heartbeat_while).

    Cancelling the calling activity does not stop the child: the worker
    thread keeps waiting and kills it at the deadline. That is bounded by the
    deadline, never indefinite.
    """

    def __init__(
        self,
        parse_target: ParseTarget,
        *,
        deadline_seconds: float,
        max_concurrency: int,
        start_method: StartMethod = "forkserver",
    ) -> None:
        if not math.isfinite(deadline_seconds) or deadline_seconds <= 0:
            raise ValueError("parse deadline must be finite and positive")
        if max_concurrency < 1:
            raise ValueError("parse concurrency must be at least one")
        self._target = parse_target
        self.deadline_seconds = deadline_seconds
        self._start_method: StartMethod = start_method
        self._slots = threading.BoundedSemaphore(max_concurrency)

    @classmethod
    def from_catalog(
        cls, parser_config_path: Path, *, deadline_seconds: float, max_concurrency: int
    ) -> IsolatedDocumentParser:
        """Parse with the registry ``parser_config_path`` configures, rebuilt per child."""

        target = functools.partial(parse_with_catalog, str(parser_config_path.resolve()))
        return cls(target, deadline_seconds=deadline_seconds, max_concurrency=max_concurrency)

    def parse(self, source: RawDocument, /) -> ParsedDocument:
        with self._slots:
            parsed = run_with_deadline(
                self._target,
                (source,),
                deadline_seconds=self.deadline_seconds,
                start_method=self._start_method,
            )
        if not isinstance(parsed, ParsedDocument):
            raise DocumentParseCrashError(
                "parse process returned something other than a parsed document"
            )
        return parsed


__all__ = [
    "IsolatedDocumentParser",
    "ParseTarget",
    "StartMethod",
    "parse_with_catalog",
    "run_with_deadline",
]
