from __future__ import annotations

import base64
import binascii
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, field_validator

from harborrag_core.base import StrictModel


class TextContentPart(StrictModel):
    """Carry one text segment in a multimodal chat message."""

    type: Literal["text"] = "text"
    text: str = Field(min_length=1)

    @field_validator("text")
    @classmethod
    def validate_text_content(cls, value: str) -> str:
        """Reject text parts containing only whitespace."""

        if not value.strip():
            raise ValueError("text content cannot be blank")
        return value


class ImageURL(StrictModel):
    """Describe an image URL and its requested provider detail level."""

    url: str = Field(min_length=1)
    detail: Literal["auto", "low", "high"] | None = None

    @field_validator("url")
    @classmethod
    def validate_image_url(cls, value: str) -> str:
        """Accept absolute HTTP(S) URLs or non-empty base64 image data URLs."""

        if value != value.strip():
            raise ValueError("image URL cannot contain surrounding whitespace")
        parsed = urlsplit(value)
        if parsed.scheme in {"http", "https"} and parsed.hostname:
            return value
        if value.startswith("data:image/"):
            metadata, separator, encoded = value.partition(",")
            if separator and metadata.endswith(";base64") and encoded:
                try:
                    decoded = base64.b64decode(encoded, validate=True)
                except (binascii.Error, ValueError) as exc:
                    raise ValueError("image data URL contains invalid base64 data") from exc
                if decoded:
                    return value
        raise ValueError("image URL must be absolute HTTP(S) or a base64 image data URL")


class ImageURLContentPart(StrictModel):
    """Carry an image URL inside a multimodal chat message."""

    type: Literal["image_url"] = "image_url"
    image_url: ImageURL


class InputAudio(StrictModel):
    """Carry encoded input audio and its declared media format."""

    data: str
    format: Literal["wav", "mp3"]


class InputAudioContentPart(StrictModel):
    """Carry audio input inside a multimodal chat message."""

    type: Literal["input_audio"] = "input_audio"
    input_audio: InputAudio


type ContentPart = TextContentPart | ImageURLContentPart | InputAudioContentPart
