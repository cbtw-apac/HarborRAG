from __future__ import annotations

from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from harborrag_runtime import parser_subprocess


def test_parser_subprocess_extracts_context_and_runs_child(monkeypatch) -> None:
    parent_context = object()
    tracer = Mock()
    tracer.start_as_current_span.return_value = nullcontext()
    provider = Mock()
    run = Mock(return_value=SimpleNamespace(returncode=17))
    monkeypatch.setenv("TRACEPARENT", "00-0123456789abcdef0123456789abcdef-0123456789abcdef-01")
    monkeypatch.setenv("TRACESTATE", "vendor=value")
    monkeypatch.setenv("HARBORRAG_DOCUMENT_EXECUTION_ID", "harborrag-document:task:0:1")
    monkeypatch.setattr(parser_subprocess, "bootstrap_tracing", Mock(return_value=provider))
    monkeypatch.setattr(parser_subprocess.propagate, "extract", Mock(return_value=parent_context))
    monkeypatch.setattr(parser_subprocess.trace, "get_tracer", Mock(return_value=tracer))
    monkeypatch.setattr(parser_subprocess.subprocess, "run", run)

    result = parser_subprocess.main(["mineru", "-p", "doc-01.pdf"])

    assert result == 17
    parser_subprocess.bootstrap_tracing.assert_called_once_with(service_name="harborrag-parser")
    parser_subprocess.propagate.extract.assert_called_once_with(
        {
            "traceparent": "00-0123456789abcdef0123456789abcdef-0123456789abcdef-01",
            "tracestate": "vendor=value",
        }
    )
    tracer.start_as_current_span.assert_called_once_with(
        "harborrag.parser.subprocess",
        context=parent_context,
        attributes={
            "harborrag.document_execution_id": "harborrag-document:task:0:1",
            "process.executable.name": "mineru",
        },
    )
    run.assert_called_once_with(["mineru", "-p", "doc-01.pdf"], check=False)
    provider.force_flush.assert_called_once_with()
    provider.shutdown.assert_called_once_with()


def test_parser_subprocess_requires_a_command() -> None:
    with pytest.raises(SystemExit, match="parser_subprocess requires a command"):
        parser_subprocess.main([])
