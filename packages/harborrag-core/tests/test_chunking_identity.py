from __future__ import annotations

import pytest

from harborrag_core.chunking import (
    CanonicalIdentityBuilder,
    canonical_identity_payload,
    encoded_identifier,
    manifest_fingerprint,
    normalize_identity_text,
    normalize_structural_path,
)
from harborrag_core.chunking.errors import ChunkContractError, ChunkIdentityError


def test_canonical_identity_policy_rejects_ambiguous_inputs_and_remains_stable():
    builder = CanonicalIdentityBuilder()

    assert normalize_identity_text("\r\nalpha\r\n\r\n\r\n beta \r\n") == "alpha\n\nbeta"
    assert manifest_fingerprint(("chunk:1", "chunk:2")) == manifest_fingerprint(
        ("chunk:1", "chunk:2")
    )
    assert builder.section_id(document_id="document:1", section_path=("A",))
    with pytest.raises(ChunkContractError, match="non-empty"):
        normalize_structural_path(("A", " "))
    with pytest.raises(ChunkIdentityError, match="finite JSON"):
        canonical_identity_payload(float("nan"))
    with pytest.raises(ChunkIdentityError, match="non-empty"):
        encoded_identifier(" ", {})
    with pytest.raises(ChunkIdentityError, match="not supported"):
        canonical_identity_payload(object())
