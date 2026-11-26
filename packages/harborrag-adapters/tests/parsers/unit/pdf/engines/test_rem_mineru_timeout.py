"""A MinerU timeout carries no chained error that quotes the command line."""

from __future__ import annotations

import os
import traceback
from pathlib import Path

import pytest

from harborrag_adapters.parsers.errors import ParseError
from harborrag_adapters.parsers.pdf.engines.mineru.config import MinerUPDFConfig
from harborrag_adapters.parsers.pdf.engines.mineru.engine import MinerUPDFEngine
from harborrag_core.domain.parser import ParseInput

pytestmark = [
    pytest.mark.unit,
    pytest.mark.skipif(os.name != "posix", reason="needs a POSIX shell script"),
]

API_URL = "http://demo-mineru.invalid:8000"


def test_mineru_timeout_drops_the_command_line(tmp_path: Path) -> None:
    script = tmp_path / "slow-mineru"
    script.write_text("#!/bin/sh\nexec sleep 30\n", encoding="utf-8")
    script.chmod(0o700)
    engine = MinerUPDFEngine(
        MinerUPDFConfig(
            executable=str(script),
            timeout_seconds=1,
            api_url=API_URL,
            extra_args=("--demo-flag", "demo-value"),
        )
    )

    with pytest.raises(ParseError, match="timed out after 1 seconds") as raised:
        engine.parse_input(ParseInput(content=b"%PDF-1.4\n%%EOF\n", filename="demo.pdf"))

    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    printed = "".join(traceback.format_exception(raised.value))
    assert API_URL not in printed
    assert "demo-value" not in printed
