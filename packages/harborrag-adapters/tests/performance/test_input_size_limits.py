"""Performance-adjacent tests for large parser inputs and size guards."""
from __future__ import annotations

import pytest

from harborrag_adapters.parsers.engine import HarborParser
from harborrag_adapters.parsers.exceptions import ParseError
from harborrag_adapters.parsers.utils import DEFAULT_MAX_INPUT_BYTES, guard_input_size
from harborrag_core.domain.parser import ParseInput


pytestmark = [pytest.mark.slow, pytest.mark.blackbox, pytest.mark.timeout(30)]


def test_multi_megabyte_text_inputs_parse() -> None:
    parser = HarborParser()

    csv_row = "1234567,alpha,beta,gamma,delta\n"
    csv_body = "a,b,c,d,e\n" + csv_row * 60_000
    assert len(csv_body.encode("utf-8")) > 1_500_000
    csv_doc = parser.parse(ParseInput(content=csv_body, filename="big.csv"))
    assert csv_doc.content
    assert csv_doc.parser_name == "csv"

    json_body = "[" + ",".join(f'"item-{i:05d}"' for i in range(80_000)) + "]"
    assert len(json_body.encode("utf-8")) > 1_000_000
    json_doc = parser.parse(ParseInput(content=json_body, filename="big.json"))
    assert json_doc.content
    assert json_doc.parser_name == "json"


def test_guard_input_size_mechanism_without_large_allocation() -> None:
    data = bytes(bytearray(1024))
    assert guard_input_size(data, max_bytes=len(data)) is data
    assert guard_input_size(data, max_bytes=len(data) + 1) is data

    with pytest.raises(ParseError) as excinfo:
        guard_input_size(data, max_bytes=len(data) - 1)
    message = str(excinfo.value)
    assert str(len(data)) in message and str(len(data) - 1) in message

    assert DEFAULT_MAX_INPUT_BYTES == 512 * 1024 * 1024
    assert DEFAULT_MAX_INPUT_BYTES >= 100 * 1024 * 1024
