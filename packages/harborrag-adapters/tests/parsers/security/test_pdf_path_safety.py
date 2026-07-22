"""PDF temp-file materialization hardening tests."""

from __future__ import annotations

import pytest

from harborrag_adapters.parsers.pdf_engine.utils import materialized_pdf_path
from harborrag_core.domain.parser import ParseInput

pytestmark = pytest.mark.blackbox


@pytest.mark.parametrize(
    "malicious_name",
    [
        "../../etc/cron.d/evil.pdf",
        "/etc/cron.d/evil.pdf",
        "..\\..\\windows\\system32\\evil.pdf",
        "....//....//evil.pdf",
    ],
)
def test_materialized_pdf_path_ignores_untrusted_filename(malicious_name: str) -> None:
    parse_input = ParseInput(content=b"%PDF-1.4 fake", filename=malicious_name)
    with materialized_pdf_path(parse_input) as path:
        assert path.name == "document.pdf"
        assert path.parent.name.startswith("harborrag-pdf-")
        assert not str(path).endswith("cron.d/evil.pdf")
        assert path.read_bytes() == b"%PDF-1.4 fake"
