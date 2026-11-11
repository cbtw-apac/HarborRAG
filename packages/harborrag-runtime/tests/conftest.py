"""Shared fixtures for the runtime suite."""

from __future__ import annotations

from collections.abc import Callable

import pytest
from temporalio.worker.workflow_sandbox import SandboxedWorkflowRunner, SandboxRestrictions

SHELL_VARIABLES = (
    "HARBORRAG_LOCAL_SOURCE_ROOT",
    "HARBORRAG_TENANT_ID",
    "HARBORRAG_INGESTION_TENANT_ID",
)


@pytest.fixture(autouse=True)
def _isolated_from_shell(monkeypatch: pytest.MonkeyPatch) -> None:
    """Drop the variables a developer's shell may export; a test that needs one sets it."""
    for name in SHELL_VARIABLES:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def sandbox_runner() -> Callable[[], SandboxedWorkflowRunner]:
    """Build a Temporal sandbox runner that passes ``beartype`` through.

    The sandbox re-imports a workflow's dependency chain in a restricted
    interpreter. ``beartype`` -- pulled in by ``fastmcp``, which the MCP
    server's suite imports into the same process -- does not survive that: its
    ``claw`` submodule is left partially initialized and the re-import fails
    with a circular ``ImportError``. Nothing about the workflows is at fault,
    and a Temporal worker never imports fastmcp, so this is an artefact of
    sharing one interpreter across suites rather than a defect these tests
    should report. Passing the module through makes the sandbox reuse the copy
    already imported, which is what passthrough is for.
    """

    restrictions = SandboxRestrictions.default.with_passthrough_modules("beartype")
    return lambda: SandboxedWorkflowRunner(restrictions=restrictions)
