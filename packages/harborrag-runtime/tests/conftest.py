"""Shared fixtures for the runtime suite."""

from __future__ import annotations

from collections.abc import Callable

import pytest
from temporalio.worker.workflow_sandbox import SandboxedWorkflowRunner, SandboxRestrictions


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


@pytest.fixture(autouse=True)
def _hermetic_connector_host_resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    """Connector config validation resolves DNS names; answer with a public address.

    Literal IP hosts never reach this seam, so private-literal checks still run for
    real. Tests that need a specific answer override ``_resolve_host`` themselves.
    """

    monkeypatch.setattr(
        "harborrag_adapters.connectors.policies.validation._resolve_host",
        lambda _hostname, _port: ("1.1.1.1",),
    )
