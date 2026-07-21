class HarborError(Exception):
    """Base exception for provider-neutral HarborRAG failures."""


class URLPolicyError(HarborError):
    """Raised when a URL violates the configured outbound-access policy."""
