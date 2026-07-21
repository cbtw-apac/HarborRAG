# `HarborError` used to be redefined here as an independent base, unrelated to
# `harborrag_core.contracts.errors.HarborError`. That split meant model-layer
# errors (HarborModelError et al.) and URLPolicyError were invisible to code
# that only knows about the contracts hierarchy -- notably the API app's
# `@app.exception_handler(HarborError)` in harborrag_app/api/errors.py, which
# only ever saw `contracts.errors.HarborError`. Re-exporting the same class
# here keeps this module's public name stable while making every subclass
# (on both sides) part of one hierarchy.
from harborrag_core.contracts.errors import HarborError

__all__ = ["HarborError", "URLPolicyError"]


class URLPolicyError(HarborError):
    """Raised when a URL violates the configured outbound-access policy."""
