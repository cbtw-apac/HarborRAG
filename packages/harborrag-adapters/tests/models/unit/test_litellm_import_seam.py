from __future__ import annotations

import sys

import litellm
import pytest

from harborrag_adapters.models.runtime.errors import (
    ModelErrorCategory,
    classify_model_exception,
)
from harborrag_adapters.models.runtime.litellm_import import (
    optional_litellm,
    require_litellm,
)
from harborrag_core.models.errors import (
    HarborChatConfigurationError,
    HarborEmbedConfigurationError,
    HarborRerankConfigurationError,
)

pytestmark = [pytest.mark.unit, pytest.mark.whitebox]


@pytest.mark.parametrize(
    "error_type",
    [
        HarborChatConfigurationError,
        HarborEmbedConfigurationError,
        HarborRerankConfigurationError,
    ],
)
def test_missing_extra_becomes_a_family_configuration_error(
    monkeypatch: pytest.MonkeyPatch,
    error_type: type[Exception],
) -> None:
    """A missing `llm` extra must not leak ImportError past the adapter boundary."""

    # A None entry in sys.modules makes `import litellm` raise ImportError, which
    # is what an uninstalled optional extra looks like from inside the seam.
    monkeypatch.setitem(sys.modules, "litellm", None)

    assert optional_litellm() is None
    with pytest.raises(error_type, match=r"harborrag-adapters\[llm\]"):
        require_litellm(error_type)


def test_installed_extra_returns_the_module() -> None:
    assert require_litellm(HarborChatConfigurationError) is litellm
    assert optional_litellm() is litellm


def test_error_classification_degrades_without_litellm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Classification keeps working on transport signals alone when LiteLLM is absent."""

    monkeypatch.setitem(sys.modules, "litellm", None)

    details = classify_model_exception(TimeoutError("slow"))
    assert details.category is ModelErrorCategory.TIMEOUT
    assert details.retryable is True

    # Without the SDK's exception hierarchy an unrecognised failure falls back to
    # PROVIDER rather than raising.
    assert classify_model_exception(ValueError("odd")).category is ModelErrorCategory.PROVIDER
