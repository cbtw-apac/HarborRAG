"""Model-profile resolution shared by ingestion and retrieval composition."""

from __future__ import annotations

from typing import Any


def embedding_dimensions(config: Any, model_name: str) -> int:
    """Resolve one unambiguous embedding dimension from the model catalog."""

    _, model = config.model_for(model_name)
    expected = {
        deployment.expected_dimensions
        for deployment in model.deployments
        if deployment.expected_dimensions is not None
    }
    if len(expected) == 1:
        return int(expected.pop())
    if model.default_params.dimensions is not None:
        return int(model.default_params.dimensions)
    raise ValueError(
        f"embedding model {model_name!r} has no unambiguous expected_dimensions; "
        "set HARBORRAG_EMBEDDING_DIMENSIONS"
    )
