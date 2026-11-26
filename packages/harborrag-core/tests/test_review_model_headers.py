"""Request headers are masked by a credential-like name, not a short list of names."""

from __future__ import annotations

import pytest
from pydantic import SecretStr, TypeAdapter

from harborrag_core.models.headers import ProtectedModelHeaders

pytestmark = pytest.mark.unit

_HEADERS = TypeAdapter(ProtectedModelHeaders)


@pytest.mark.parametrize(
    "name",
    ["X-Goog-Api-Key", "X-Auth-Token", "Cookie", "X-Client-Secret", "X-Amz-Signature"],
)
def test_credential_like_request_header_is_masked(name: str) -> None:
    headers = _HEADERS.validate_python({name: "synthetic-credential-7c1d"})

    assert isinstance(headers[name], SecretStr)
    assert "synthetic-credential-7c1d" not in repr(headers)


def test_ordinary_request_header_stays_plain() -> None:
    assert _HEADERS.validate_python({"X-Team": "rag"}) == {"X-Team": "rag"}
