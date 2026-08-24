"""API-facing HarborError subclasses"""

import pytest

from harborrag_core.contracts.errors import (
    HarborAuthError,
    HarborConflictError,
    HarborError,
    HarborNotFoundError,
    HarborSecretDecryptionError,
    HarborValidationError,
)


@pytest.mark.whitebox
@pytest.mark.parametrize(
    "exc_type",
    [
        HarborNotFoundError,
        HarborValidationError,
        HarborConflictError,
        HarborAuthError,
        HarborSecretDecryptionError,
    ],
)
def test_new_errors_subclass_harbor_error(exc_type: type[HarborError]) -> None:
    """API-facing errors is part of the HarborError hierarchy"""
    assert issubclass(exc_type, HarborError)


@pytest.mark.whitebox
def test_validation_error_carries_details() -> None:
    """HarborValidationError exposes a details dict for field-level errors,
    defaulting to empty so the envelope's `details` key is always present."""
    exc = HarborValidationError("bad payload", details={"field": "name"})
    assert exc.details == {"field": "name"}
    assert HarborValidationError("bad payload").details == {}


@pytest.mark.whitebox
def test_auth_error_distiguishes_forbidden() -> None:
    """The `forbidden` flag splits authn (401, default)
    from authz (403)"""
    assert HarborAuthError("no token").forbidden is False
    assert HarborAuthError("wrong role", forbidden=True).forbidden is True
