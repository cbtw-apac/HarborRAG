from __future__ import annotations

import pytest
from pydantic import ValidationError

from harborrag_adapters.models.chat import HarborChatClient
from harborrag_core.models.capabilities import HarborChatCapabilities
from harborrag_core.models.chat import (
    HarborChatMessage,
    ImageURL,
    ImageURLContentPart,
    TextContentPart,
)
from harborrag_core.models.errors import HarborChatCapabilityError

from .chat_client_support import FakeInvocation, response_dict

pytestmark = [pytest.mark.unit, pytest.mark.graybox]


def _with_multimodal_capability(base_config):
    logical = base_config.models["primary"]
    deployment = logical.deployments[0].model_copy(
        update={"capabilities": HarborChatCapabilities(multimodal=True)}
    )
    model = logical.model_copy(update={"deployments": (deployment,)})
    return base_config.model_copy(update={"models": {**base_config.models, "primary": model}})


def test_image_and_text_parts_are_normalized_for_litellm(base_config) -> None:
    config = _with_multimodal_capability(base_config)
    invocation = FakeInvocation([response_dict("an image")])

    response = HarborChatClient(config, invocation=invocation).chat(
        [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Describe this image"},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": "https://images.example.com/chart.png",
                            "detail": "high",
                        },
                    },
                ],
            }
        ]
    )

    assert response.text == "an image"
    assert invocation.calls[0]["messages"] == [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "Describe this image"},
                {
                    "type": "image_url",
                    "image_url": {
                        "url": "https://images.example.com/chart.png",
                        "detail": "high",
                    },
                },
            ],
        }
    ]


def test_multimodal_request_requires_deployment_capability(base_config) -> None:
    invocation = FakeInvocation([response_dict()])
    message = HarborChatMessage.user(
        (
            TextContentPart(text="Describe"),
            ImageURLContentPart(image_url=ImageURL(url="https://example.com/image.png")),
        )
    )

    with pytest.raises(HarborChatCapabilityError, match="multimodal messages"):
        HarborChatClient(base_config, invocation=invocation).chat([message])

    assert invocation.calls == []


@pytest.mark.parametrize(
    "invalid_url",
    [
        "file:///tmp/private.png",
        "ftp://example.com/image.png",
        "data:text/plain;base64,aGVsbG8=",
        "data:image/png;base64,not-base64!",
    ],
)
def test_invalid_image_media_is_rejected(invalid_url) -> None:
    with pytest.raises(ValidationError, match="image"):
        ImageURL(url=invalid_url)


def test_blank_text_and_empty_multimodal_content_are_rejected() -> None:
    with pytest.raises(ValidationError, match="blank"):
        TextContentPart(text="   ")
    with pytest.raises(ValidationError, match="at least one part"):
        HarborChatMessage.user(())


def test_valid_base64_image_data_url_is_preserved(base_config) -> None:
    config = _with_multimodal_capability(base_config)
    invocation = FakeInvocation([response_dict("pixel")])
    url = "data:image/png;base64,iVBORw0KGgo="
    message = HarborChatMessage.user((ImageURLContentPart(image_url=ImageURL(url=url)),))

    HarborChatClient(config, invocation=invocation).chat([message])

    assert invocation.calls[0]["messages"][0]["content"][0]["image_url"]["url"] == url
