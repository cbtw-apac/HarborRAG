"""Repository-wide pytest setup, loaded before every package's own conftest."""

from __future__ import annotations

import sys


def _import_langchain_without_transformers() -> None:
    """Import ``langchain_core`` while ``transformers`` is hidden from it.

    ``langchain_core.language_models.base`` tries ``from transformers import
    GPT2TokenizerFast`` at import time, only to count tokens when a model does
    not, and ``transformers`` imports ``torch`` on the way in. With the Docling
    extras installed that put about 550 MB into every pytest worker that touches
    a LangChain model, which no test uses. Hiding the module makes the import
    raise ImportError, which LangChain records once and handles; the block is
    lifted straight after, so Docling and anything else can still import
    ``transformers`` normally.
    """

    if "transformers" in sys.modules:
        return
    sys.modules["transformers"] = None  # type: ignore[assignment]
    try:
        import langchain_core.language_models.base  # noqa: F401
    except ImportError:
        pass  # LangChain is optional for some installs; nothing to pre-import.
    finally:
        if sys.modules.get("transformers", ...) is None:
            del sys.modules["transformers"]


_import_langchain_without_transformers()
