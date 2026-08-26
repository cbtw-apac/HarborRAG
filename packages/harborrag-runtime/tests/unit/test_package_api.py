from __future__ import annotations

import harborrag_runtime
from harborrag_runtime.composition import CompositionRoot


def test_package_facade_resolves_composition_root_lazily() -> None:
    assert harborrag_runtime.CompositionRoot is CompositionRoot
    assert harborrag_runtime.__all__ == [
        "CompositionRoot",
        "HarborRAG",
        "HarborRAGConfig",
    ]
