from __future__ import annotations

import harborrag_runtime
from harborrag_runtime.composition import CompositionRoot
from harborrag_runtime.temporal.client import TemporalRuntimeClient
from harborrag_runtime.temporal.lifecycle import RuntimeLifecycle


def test_package_facade_resolves_runtime_entry_points_lazily() -> None:
    assert harborrag_runtime.CompositionRoot is CompositionRoot
    assert harborrag_runtime.RuntimeLifecycle is RuntimeLifecycle
    assert harborrag_runtime.TemporalRuntimeClient is TemporalRuntimeClient
