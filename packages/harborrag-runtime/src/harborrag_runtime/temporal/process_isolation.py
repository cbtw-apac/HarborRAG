"""Killable, resource-bounded execution for untrusted document processing."""

from __future__ import annotations

import asyncio
import multiprocessing
import os
import signal
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from multiprocessing.process import BaseProcess
from typing import Any, cast

import cloudpickle

from harborrag_runtime.temporal.process_codec import (
    ProcessResultKind,
    decode_process_response,
    encode_process_error,
    encode_process_result,
)

_PROCESS_POLL_INTERVAL_SECONDS = 0.05


@dataclass(frozen=True, slots=True)
class ProcessLimits:
    """Hard per-document limits inherited by parser subprocess descendants."""

    wall_seconds: float = 900.0
    cpu_seconds: int = 600
    address_space_bytes: int = 8 * 1024 * 1024 * 1024
    file_size_bytes: int = 512 * 1024 * 1024
    open_files: int = 256
    max_payload_bytes: int = 768 * 1024 * 1024
    max_result_bytes: int = 1024 * 1024 * 1024

    def __post_init__(self) -> None:
        values = (
            self.wall_seconds,
            self.cpu_seconds,
            self.address_space_bytes,
            self.file_size_bytes,
            self.open_files,
            self.max_payload_bytes,
            self.max_result_bytes,
        )
        if any(value <= 0 for value in values):
            raise ValueError("isolated process limits must be positive")


@dataclass(slots=True)
class IsolatedProcessRunner:
    """Run trusted parser/chunker callables in disposable spawned processes."""

    limits: ProcessLimits = ProcessLimits()
    max_concurrency: int = 2
    _semaphore: asyncio.Semaphore = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if self.max_concurrency < 1:
            raise ValueError("isolated process concurrency must be positive")
        self._semaphore = asyncio.Semaphore(self.max_concurrency)

    async def run[ResultT](
        self,
        call: Callable[..., ResultT],
        *args: object,
        heartbeat: Callable[[], None],
        heartbeat_interval_seconds: float,
        result_kind: ProcessResultKind = ProcessResultKind.JSON,
    ) -> ResultT:
        """Execute one call with periodic heartbeats and kill-on-cancel semantics."""

        if heartbeat_interval_seconds <= 0:
            raise ValueError("heartbeat interval must be positive")
        payload = cloudpickle.dumps((call, args))
        if len(payload) > self.limits.max_payload_bytes:
            raise ValueError("isolated process input exceeds the configured byte limit")
        async with self._semaphore:
            return await self._run_process(
                payload,
                heartbeat=heartbeat,
                heartbeat_interval_seconds=heartbeat_interval_seconds,
                result_kind=result_kind,
            )

    async def _run_process[ResultT](
        self,
        payload: bytes,
        *,
        heartbeat: Callable[[], None],
        heartbeat_interval_seconds: float,
        result_kind: ProcessResultKind,
    ) -> ResultT:
        context = multiprocessing.get_context("spawn")
        parent_connection, child_connection = context.Pipe(duplex=False)
        process = context.Process(
            target=_process_main,
            args=(
                child_connection,
                payload,
                self.limits,
                _allowed_environment(),
                result_kind,
            ),
            daemon=False,
        )
        process.start()
        child_connection.close()
        deadline = time.monotonic() + self.limits.wall_seconds
        next_heartbeat = time.monotonic() + heartbeat_interval_seconds
        try:
            while True:
                if parent_connection.poll():
                    message = parent_connection.recv_bytes(
                        self.limits.max_result_bytes + 1024 * 1024
                    )
                    kind, value = decode_process_response(message, result_kind)
                    if kind == "result":
                        return cast("ResultT", value)
                    raise _isolated_error(cast("dict[str, str]", value))
                if not process.is_alive():
                    process.join()
                    if parent_connection.poll():
                        continue
                    raise RuntimeError(
                        f"isolated document worker exited with code {process.exitcode}"
                    )
                now = time.monotonic()
                if now >= deadline:
                    _kill_process_group(process)
                    raise TimeoutError("isolated document processing exceeded its wall timeout")
                if now >= next_heartbeat:
                    heartbeat()
                    next_heartbeat = now + heartbeat_interval_seconds
                await asyncio.sleep(
                    min(
                        _PROCESS_POLL_INTERVAL_SECONDS,
                        max(0.0, deadline - now),
                        max(0.0, next_heartbeat - now),
                    )
                )
        except BaseException:
            _kill_process_group(process)
            raise
        finally:
            parent_connection.close()
            _kill_process_group(process)


def _process_main(
    connection: Any,
    payload: bytes,
    limits: ProcessLimits,
    environment: dict[str, str],
    result_kind: ProcessResultKind = ProcessResultKind.JSON,
) -> None:
    """Child entry point; sanitize privileges before deserializing the callable."""

    try:
        if os.name == "posix":
            os.setsid()
        os.environ.clear()
        os.environ.update(environment)
        os.environ["HARBORRAG_ISOLATED_PROCESS"] = "1"
        _apply_resource_limits(limits)
        call, args = cloudpickle.loads(payload)
        result = call(*args)
        response = encode_process_result(result_kind, result)
        if len(response) > limits.max_result_bytes:
            raise MemoryError("isolated process result exceeds the configured byte limit")
        connection.send_bytes(response)
    except BaseException as exc:
        response = encode_process_error(exc)
        try:
            connection.send_bytes(response)
        except (BrokenPipeError, EOFError, OSError):
            pass
    finally:
        connection.close()


def _apply_resource_limits(limits: ProcessLimits) -> None:
    if os.name != "posix":
        return
    import resource

    configured = (
        (resource.RLIMIT_CPU, limits.cpu_seconds),
        (resource.RLIMIT_AS, limits.address_space_bytes),
        (resource.RLIMIT_FSIZE, limits.file_size_bytes),
        (resource.RLIMIT_NOFILE, limits.open_files),
    )
    for resource_name, requested in configured:
        current_soft, current_hard = resource.getrlimit(resource_name)
        hard = requested if current_hard == resource.RLIM_INFINITY else min(requested, current_hard)
        soft = min(requested, hard)
        resource.setrlimit(resource_name, (soft, hard))


def _allowed_environment() -> dict[str, str]:
    names = {
        "PATH",
        "LANG",
        "LC_ALL",
        "TMPDIR",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "REQUESTS_CA_BUNDLE",
        "CUDA_VISIBLE_DEVICES",
        "HF_HOME",
        "TORCH_HOME",
        "XDG_CACHE_HOME",
    }
    return {name: os.environ[name] for name in names if name in os.environ}


def _kill_process_group(process: BaseProcess) -> None:
    pid = process.pid
    if pid is None:
        if process.is_alive():
            process.kill()
        process.join()
        return
    if os.name == "posix":
        try:
            # The group may still contain a parser descendant after its leader
            # has exited, so address it by the reserved leader PID directly.
            os.killpg(pid, signal.SIGKILL)
        except ProcessLookupError:
            if process.is_alive():
                process.kill()
    elif process.is_alive():
        process.kill()
    process.join()


def _isolated_error(value: dict[str, str]) -> Exception:
    error_type = value.get("type", "Exception")
    if error_type == "UnsupportedFormatError":
        from harborrag_adapters.parsers.errors import UnsupportedFormatError

        return UnsupportedFormatError("unsupported format in isolated parser")
    if error_type == "EncryptedPdfError":
        from harborrag_adapters.parsers.errors import EncryptedPdfError

        return EncryptedPdfError("encrypted PDF rejected by isolated parser")
    if value.get("module", "").startswith("harborrag_adapters.parsers"):
        from harborrag_adapters.parsers.errors import ParseError

        return ParseError(f"{error_type} in isolated parser")
    return RuntimeError(f"{error_type} in isolated document worker")
