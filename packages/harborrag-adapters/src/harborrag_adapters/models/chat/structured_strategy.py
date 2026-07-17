from enum import StrEnum


class StructuredOutputStrategy(StrEnum):
    """Select how a typed response schema is enforced for one chat request."""

    AUTO = "auto"
    NATIVE_SCHEMA = "native_schema"
    JSON_MODE = "json_mode"
    PROMPT_FALLBACK = "prompt_fallback"
