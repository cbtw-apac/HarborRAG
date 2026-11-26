from __future__ import annotations

import pytest
from pydantic import ValidationError

from harborrag_adapters.models.embed import HarborEmbedClientConfig

pytestmark = [pytest.mark.unit, pytest.mark.whitebox]


def _config(embedding_space: str) -> HarborEmbedClientConfig:
    return HarborEmbedClientConfig.from_dict(
        {
            "embed": {
                "default_model": "primary",
                "models": {
                    "primary": {
                        "embedding_space": embedding_space,
                        "provider": "openai",
                        "model": "embed-test",
                        "api_key": "embed-secret",
                    }
                },
            }
        }
    )


@pytest.mark.parametrize(
    "padded", ["embed-test\r", "embed-test\n", " embed-test ", "\tembed-test\r\n"]
)
def test_embedding_space_loses_surrounding_whitespace_and_line_breaks(padded: str) -> None:
    assert _config(padded).models["primary"].embedding_space == "embed-test"


def test_embedding_space_with_an_inner_line_break_is_refused() -> None:
    with pytest.raises(ValidationError, match="embedding_space must not contain line breaks"):
        _config("embed\r\ntest")


@pytest.mark.parametrize("blank", ["", "  ", "\r\n"])
def test_blank_embedding_space_is_refused(blank: str) -> None:
    with pytest.raises(ValidationError, match="embedding_space"):
        _config(blank)
