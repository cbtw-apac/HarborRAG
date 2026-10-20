"""Traced launcher for parser CLIs that run outside the worker process."""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Sequence

from opentelemetry import propagate, trace

from harborrag_runtime.observability.tracing import bootstrap_tracing


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments and arguments[0] == "--":
        arguments = arguments[1:]
    if not arguments:
        raise SystemExit("parser_subprocess requires a command")

    provider = bootstrap_tracing(service_name="harborrag-parser")
    carrier = {
        key: value
        for key, value in (
            ("traceparent", os.environ.get("TRACEPARENT")),
            ("tracestate", os.environ.get("TRACESTATE")),
        )
        if value
    }
    parent = propagate.extract(carrier)
    tracer = trace.get_tracer("harborrag.parser")
    try:
        with tracer.start_as_current_span(
            "harborrag.parser.subprocess",
            context=parent,
            attributes={
                "harborrag.document_execution_id": os.environ.get(
                    "HARBORRAG_DOCUMENT_EXECUTION_ID", "unknown"
                ),
                "process.executable.name": os.path.basename(arguments[0]),
            },
        ):
            return subprocess.run(arguments, check=False).returncode
    finally:
        provider.force_flush()
        provider.shutdown()


if __name__ == "__main__":
    raise SystemExit(main())
