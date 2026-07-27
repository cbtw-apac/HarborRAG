from __future__ import annotations

import pytest
from model_runtime_support import embed_config

from harborrag_adapters.models.embed.validation import validate_embed_request
from harborrag_core.models.capabilities import HarborEmbedCapabilities
from harborrag_core.models.embed import (
    EmbeddingEncodingFormat,
    EmbeddingPurpose,
    HarborEmbedRequest,
)
from harborrag_core.models.errors import (
    HarborEmbedCapabilityError,
    HarborEmbedInvalidRequestError,
)

from .conftest import deployment

pytestmark = [pytest.mark.unit, pytest.mark.whitebox]


def test_embedding_request_capability_and_security_validation() -> None:
    limited = deployment(
        capabilities=HarborEmbedCapabilities(
            batch=True, default_dimensions=3, encoding_format=False
        )
    )
    config = embed_config(deployments=(limited,))
    same_dimensions = HarborEmbedRequest(inputs=("x",), dimensions=3)
    assert validate_embed_request(same_dimensions, config, limited).dimensions is None
    float_format = HarborEmbedRequest(inputs=("x",), encoding_format=EmbeddingEncodingFormat.FLOAT)
    assert validate_embed_request(float_format, config, limited).encoding_format is None
    invalid = [
        HarborEmbedRequest(inputs=((1, 2),)),
        HarborEmbedRequest(inputs=("x",), dimensions=2),
        HarborEmbedRequest(inputs=("x",), encoding_format=EmbeddingEncodingFormat.BASE64),
        HarborEmbedRequest(inputs=("x",), purpose=EmbeddingPurpose.QUERY),
        HarborEmbedRequest(inputs=("x",), extra_params={"model": "bad"}),
        HarborEmbedRequest(inputs=("x",), custom_headers={"Authorization": "bad"}),
    ]
    for request in invalid:
        with pytest.raises((HarborEmbedCapabilityError, HarborEmbedInvalidRequestError)):
            validate_embed_request(request, config, limited)
    tiny = config.model_copy(update={"max_inputs_per_request": 1, "max_characters_per_input": 1})
    with pytest.raises(HarborEmbedInvalidRequestError):
        validate_embed_request(HarborEmbedRequest(inputs=("a", "b")), tiny, limited)
    with pytest.raises(HarborEmbedInvalidRequestError):
        validate_embed_request(HarborEmbedRequest(inputs=("too long",)), tiny, limited)
