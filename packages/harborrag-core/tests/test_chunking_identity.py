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


def test_canonical_identity_payload_rejects_non_string_keys_and_cycles():
    with pytest.raises(ChunkIdentityError, match="must be strings"):
        canonical_identity_payload({1: "x"})

    with pytest.raises(ChunkIdentityError, match="unique"):
        canonical_identity_payload({"a ": 1, "a": 2})

    cyclic_list: list[object] = []
    cyclic_list.append(cyclic_list)
    with pytest.raises(ChunkIdentityError, match="cycles"):
        canonical_identity_payload(cyclic_list)

    cyclic_dict: dict[str, object] = {}
    cyclic_dict["self"] = cyclic_dict
    with pytest.raises(ChunkIdentityError, match="cycles"):
        canonical_identity_payload(cyclic_dict)

    shared = {"value": 1}
    assert canonical_identity_payload({"a": shared, "b": shared})
