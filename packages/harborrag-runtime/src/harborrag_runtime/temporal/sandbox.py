"""Temporal sandbox configuration for the ingestion workflow workers."""

from __future__ import annotations

import sys
from collections.abc import Iterable, Mapping
from types import ModuleType

from temporalio.worker.workflow_sandbox import SandboxedWorkflowRunner, SandboxRestrictions

_HARBORRAG_PACKAGE_PREFIX = "harborrag_"


def workflow_sandbox_runner(
    workflows: Iterable[type],
    *,
    loaded_modules: Mapping[str, ModuleType] | None = None,
) -> SandboxedWorkflowRunner:
    """Pass already-loaded HarborRAG modules through the workflow sandbox.

    The default sandbox re-executes every HarborRAG module a workflow imports, and
    rebuilds their pydantic models, on every workflow run: about 320ms of CPU per
    document workflow.

    The passthrough is configured on the runner rather than written as
    ``workflow.unsafe.imports_passed_through()`` in the workflow modules. The
    sandbox's ``sys.modules`` lazily serves only configured modules, and
    ``typing.get_type_hints`` reads it whenever Temporal decodes a dataclass input:
    ``SourceIngestionInput`` inherits fields annotated in
    ``harborrag_runtime.ingestion_contracts``, a module no workflow imports by name.
    With the in-module form, that lookup found nothing and every source workflow
    failed with ``NameError: name 'ProcessingProfileInput' is not defined``.

    The workflow modules and their parent packages stay sandboxed. Passthrough
    matches by package prefix, so passing a parent through would also pass through
    its workflow modules, which would turn off the sandbox's determinism checks
    for workflow code.
    """

    modules = sys.modules if loaded_modules is None else loaded_modules
    sandboxed: set[str] = set()
    for workflow_type in workflows:
        parts = workflow_type.__module__.split(".")
        sandboxed.update(".".join(parts[:end]) for end in range(1, len(parts) + 1))
    passthrough = sorted(
        name
        for name in tuple(modules)
        if name.startswith(_HARBORRAG_PACKAGE_PREFIX) and name not in sandboxed
    )
    return SandboxedWorkflowRunner(
        restrictions=SandboxRestrictions.default.with_passthrough_modules(*passthrough)
    )
