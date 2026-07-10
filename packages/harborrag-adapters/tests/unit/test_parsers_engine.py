from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any, ClassVar

import pytest
from harborrag_adapters.parsers import (
    PARSER_LOGGER_NAME,
    BaseParser,
    CsvParser,
    DoclingBackend,
    DoclingBackendOptions,
    HarborParser,
    HtmlParser,
    JsonParser,
    LiteParseBackend,
    LiteParseBackendOptions,
    MarkdownParser,
    MinerUBackend,
    MinerUBackendOptions,
    PaddleOcrBackend,
    PaddleOcrBackendOptions,
    PdfBackend,
    PdfParser,
    PdfParseResult,
    PdfParserProfile,
    TextParser,
    UnsupportedFormatError,
    get_parser_logger,
    parser_log_extra,
)
from harborrag_adapters.parsers.pdf_engine.utils import content_from_any
from harborrag_core.domain.element import DocumentElement
from harborrag_core.domain.parser import ParsedDocument, ParseInput

pytestmark = pytest.mark.unit


class FakeParser(BaseParser[ParseInput, ParsedDocument]):
    parser_name: ClassVar[str] = "fake"
    parser_engine: ClassVar[str] = "test-engine"
    suffixes: ClassVar[frozenset[str]] = frozenset({"fake"})
    content_types: ClassVar[frozenset[str]] = frozenset({"application/x-fake"})

    def parse(self, input: ParseInput) -> ParsedDocument:
        parse_input = self.coerce_input(input)
        content = parse_input.read_text()
        return ParsedDocument(
            content=content,
            parser_name=self.parser_name,
            parser_version=self.parser_version,
            elements=[DocumentElement(id="fake:0", type="paragraph", content=content)],
            metadata=self.metadata_for(parse_input),
        )


class OtherFakeParser(FakeParser):
    parser_name: ClassVar[str] = "other_fake"
    content_types: ClassVar[frozenset[str]] = frozenset({"application/x-other-fake"})


class BuggyParser(FakeParser):
    parser_name: ClassVar[str] = "buggy"
    suffixes: ClassVar[frozenset[str]] = frozenset({"buggy"})
    content_types: ClassVar[frozenset[str]] = frozenset({"application/x-buggy"})

    def parse(self, parse_input: ParseInput) -> ParsedDocument:
        raise TypeError("implementation bug")


class EmptyPdfBackend(PdfBackend):
    name: ClassVar[str] = "empty_pdf"

    def parse(self, input: ParseInput) -> PdfParseResult:
        return PdfParseResult(content="", engine=self.name)


class UsefulPdfBackend(PdfBackend):
    name: ClassVar[str] = "useful_pdf"

    def parse(self, input: ParseInput) -> PdfParseResult:
        content = "Useful PDF content for downstream retrieval"
        return PdfParseResult(
            content=content,
            engine=self.name,
            elements=[
                DocumentElement(id="pdf:useful:0", type="paragraph", content=content)
            ],
            metadata={"page_count": 1},
        )


def test_parser_package_smoke_imports_and_default_registry():
    parser = HarborParser()

    assert parser.create("markdown").name == "markdown"
    assert parser.create("csv").name == "csv"
    assert parser.create("pdf").name == "pdf"
    assert parser.create("text").name == "text"
    assert parser.parser_for(ParseInput(content="# Hi", filename="doc.md")).name == (
        "markdown"
    )
    assert parser.parser_for(ParseInput(content=b"%PDF", filename="doc.pdf")).name == (
        "pdf"
    )
    assert get_parser_logger().name == PARSER_LOGGER_NAME
    assert get_parser_logger("Registry").name == ("harborrag.adapters.parsers.registry")
    assert any(
        isinstance(handler, logging.NullHandler)
        for handler in get_parser_logger().handlers
    )


@pytest.mark.whitebox
def test_registry_indexes_routes_and_rejects_duplicate_ownership():
    registry = HarborParser([])
    fake = FakeParser()
    registry.register(fake)

    assert registry._by_name["fake"] is fake
    assert registry._by_suffix[".fake"] is fake
    assert registry._by_content_type["application/x-fake"] is fake

    with pytest.raises(ValueError, match="already registered"):
        registry.register(OtherFakeParser())

    registry.unregister("fake")
    assert registry.parser_for(ParseInput(content="hello", filename="doc.fake")) is None


@pytest.mark.whitebox
def test_registry_rejects_conflicting_suffix_and_content_type_routes():
    registry = HarborParser([FakeParser(), HtmlParser()])
    ambiguous = ParseInput(
        content="<p>hello</p>",
        filename="doc.fake",
        content_type="text/html",
    )

    with pytest.raises(UnsupportedFormatError, match="Conflicting parser routes"):
        registry.parse(ambiguous)


def test_unexpected_parser_bug_is_not_normalized_or_skipped():
    registry = HarborParser([BuggyParser()])
    parse_input = ParseInput(content="data", filename="doc.buggy")

    with pytest.raises(TypeError, match="implementation bug"):
        registry.parse_many([parse_input], on_error="skip")


@pytest.mark.whitebox
def test_pdf_result_normalizer_keeps_ocr_text_without_scores():
    raw_ocr = [
        [
            ([[0, 0], [1, 0], [1, 1], [0, 1]], ("Hello", 0.99)),
            ([[0, 2], [1, 2], [1, 3], [0, 3]], ("World", 0.98)),
        ]
    ]

    assert content_from_any(raw_ocr) == "Hello\nWorld"


@pytest.mark.whitebox
def test_content_from_any_does_not_recurse_forever_on_self_reference():
    cyclic: dict[str, Any] = {"text": "leaf value"}
    cyclic["self"] = cyclic

    # Must not raise RecursionError; the cycle guard should short-circuit
    # once the object has already been visited.
    assert content_from_any(cyclic) == "leaf value"


@pytest.mark.whitebox
def test_content_from_any_bails_out_on_pathologically_deep_nesting():
    nested: Any = "leaf value"
    for _ in range(1000):
        nested = {"content": nested}

    # Must not raise RecursionError; deep nesting past the walk-depth cap
    # bails out cleanly instead of blowing the Python call stack.
    content_from_any(nested)


@pytest.mark.graybox
def test_parser_registry_logs_route_and_result(caplog):
    caplog.set_level(logging.DEBUG, logger=PARSER_LOGGER_NAME)
    registry = HarborParser([FakeParser()])

    document = registry.parse(
        ParseInput(
            content="hello",
            filename="doc.fake",
            content_type="application/x-fake",
        )
    )

    assert document.content == "hello"
    started = next(
        record for record in caplog.records if record.msg.startswith("Parsing")
    )
    finished = next(
        record for record in caplog.records if record.msg.startswith("Parsed")
    )
    assert started.name == "harborrag.adapters.parsers.registry"
    assert started.getMessage() == "Parsing doc.fake with fake via suffix=.fake"
    assert finished.getMessage() == (
        "Parsed doc.fake with parser fake content_chars=5 elements=1"
    )


@pytest.mark.graybox
def test_parser_log_extra_uses_safe_metadata_only():
    parse_input = ParseInput(
        content="secret body",
        filename="doc.md",
        content_type="text/markdown",
    )

    extra = parser_log_extra(
        input=parse_input,
        parser_name="markdown",
        parser_engine="python/regex",
        content_chars=11,
    )

    assert extra == {
        "harbor_parser": "markdown",
        "harbor_parser_engine": "python/regex",
        "harbor_input": "doc.md",
        "harbor_suffix": ".md",
        "harbor_content_type": "text/markdown",
        "harbor_content_chars": 11,
    }
    assert "secret body" not in repr(extra)


@pytest.mark.graybox
def test_pdf_parser_falls_back_until_backend_returns_usable_content(caplog):
    caplog.set_level(logging.DEBUG, logger=PARSER_LOGGER_NAME)
    parser = PdfParser(
        backends=[EmptyPdfBackend(), UsefulPdfBackend()],
        min_content_chars=10,
    )

    document = parser.parse(ParseInput(content=b"%PDF", filename="doc.pdf"))

    assert document.content == "Useful PDF content for downstream retrieval"
    assert document.metadata["pdf_engine"] == "useful_pdf"
    assert document.metadata["page_count"] == 1
    assert document.warnings == [
        "empty_pdf: extracted less than 10 characters",
    ]
    assert any(
        record.harbor_parser_engine == "empty_pdf"
        for record in caplog.records
        if hasattr(record, "harbor_parser_engine")
    )


@pytest.mark.whitebox
def test_pdf_quality_profile_builds_expected_advanced_backends():
    backends = PdfParser.default_backends(PdfParserProfile.QUALITY)

    assert [backend.name for backend in backends] == [
        "docling",
        "mineru",
        "paddleocr",
        "pymupdf",
        "liteparse",
    ]
    assert backends[0].options.do_ocr is True
    assert backends[0].options.do_table_structure is True
    assert backends[1].options.backend == "hybrid"
    assert backends[1].options.effort == "medium"
    assert backends[2].options.use_formula_recognition is True
    assert backends[2].options.use_region_detection is True


@pytest.mark.whitebox
def test_docling_backend_options_build_convert_kwargs_without_importing_docling():
    options = DoclingBackendOptions(
        max_num_pages=3,
        max_file_size=2048,
        page_range=(1, 2),
        force_full_page_ocr=True,
        extra_convert_options={"custom": "value"},
    )
    configured = DoclingBackend(options, strict_text=True)

    assert configured.options.strict_text is True
    assert configured.options.force_full_page_ocr is True
    assert configured._convert_kwargs() == {
        "raises_on_error": True,
        "max_num_pages": 3,
        "max_file_size": 2048,
        "page_range": (1, 2),
        "custom": "value",
    }


def _encrypted_pdf_bytes() -> bytes:
    import fitz

    doc = fitz.open()
    try:
        doc.new_page().insert_text((72, 72), "secret content here")
        return doc.tobytes(
            encryption=fitz.PDF_ENCRYPT_AES_256,
            owner_pw="o",
            user_pw="u",
        )
    finally:
        doc.close()


@pytest.mark.whitebox
def test_docling_backend_rejects_encrypted_pdf_without_invoking_converter():
    from harborrag_adapters.parsers.exceptions import EncryptedPdfError

    class _ExplodingConverter:
        def convert(self, *args: Any, **kwargs: Any) -> Any:
            raise AssertionError(
                "Docling converter must not run on an encrypted PDF; the "
                "PyMuPDF pre-check should short-circuit first."
            )

    backend = DoclingBackend(DoclingBackendOptions(converter=_ExplodingConverter()))

    with pytest.raises(EncryptedPdfError):
        backend.parse(ParseInput(content=_encrypted_pdf_bytes(), filename="secret.pdf"))


@pytest.mark.whitebox
def test_docling_backend_pre_check_does_not_block_normal_pdfs():
    calls: list[str] = []

    class _RecordingConverter:
        def convert(self, *args: Any, **kwargs: Any) -> Any:
            calls.append("converted")
            return SimpleNamespace(document=SimpleNamespace())

    backend = DoclingBackend(DoclingBackendOptions(converter=_RecordingConverter()))
    import fitz

    plain_pdf = fitz.open()
    try:
        plain_pdf.new_page().insert_text((72, 72), "not secret")
        content = plain_pdf.tobytes()
    finally:
        plain_pdf.close()

    backend.parse(ParseInput(content=content, filename="plain.pdf"))

    assert calls == ["converted"]


class FakeLiteParse:
    def __init__(self) -> None:
        self.input: str | bytes | None = None

    def parse(self, input: str | bytes) -> SimpleNamespace:
        self.input = input
        return SimpleNamespace(
            text="Hello\nWorld",
            pages=[
                {
                    "page_num": 1,
                    "text_items": [{"text": "Hello"}, {"text": "World"}],
                }
            ],
        )


@pytest.mark.whitebox
def test_liteparse_backend_uses_llamaindex_liteparse_api_shape():
    fake_parser = FakeLiteParse()

    backend = LiteParseBackend(
        LiteParseBackendOptions(
            parser=fake_parser,
            output_format="markdown",
            ocr_enabled=False,
            target_pages="1-2",
        )
    )

    document = backend.parse(ParseInput(content=b"%PDF", filename="report.pdf"))

    assert Path(str(fake_parser.input)).name == "document.pdf"
    assert document.content == "Hello\nWorld"
    assert document.metadata["liteparse_output_format"] == "markdown"
    assert document.metadata["liteparse_ocr_enabled"] is False
    assert document.metadata["liteparse_target_pages"] == "1-2"
    assert document.metadata["page_count"] == 1
    assert document.elements[0].content == "Hello\nWorld"
    assert document.elements[0].metadata["page"] == 1


@pytest.mark.whitebox
def test_liteparse_backend_treats_empty_text_as_empty_not_missing():
    class _FakeEmptyLiteParse:
        def parse(self, _input: str | bytes) -> SimpleNamespace:
            # `text == ""` is a genuine empty extraction; it must not be
            # treated the same as `text is None` and fall back to dumping
            # the whole result object as content.
            return SimpleNamespace(text="", pages=[])

    backend = LiteParseBackend(LiteParseBackendOptions(parser=_FakeEmptyLiteParse()))

    document = backend.parse(ParseInput(content=b"%PDF", filename="empty.pdf"))

    assert document.content == ""


@pytest.mark.whitebox
def test_liteparse_constructor_kwargs_match_documented_python_options():
    backend = LiteParseBackend(
        LiteParseBackendOptions(
            output_format="markdown",
            image_mode="off",
            extract_links=False,
            ocr_enabled=False,
            ocr_language="fra",
            target_pages="1-5",
            dpi=300,
            extra_options={"custom_option": "value"},
        )
    )

    assert backend._constructor_kwargs() == {
        "output_format": "markdown",
        "image_mode": "off",
        "extract_links": False,
        "ocr_enabled": False,
        "ocr_language": "fra",
        "target_pages": "1-5",
        "dpi": 300,
        "custom_option": "value",
    }


@pytest.mark.whitebox
def test_mineru_command_includes_advanced_cli_options():
    backend = MinerUBackend(
        MinerUBackendOptions(
            backend="hybrid-http-client",
            effort="high",
            method="ocr",
            language="ch",
            api_url="http://127.0.0.1:8000",
            server_url="http://127.0.0.1:30000",
            extra_args=("--debug", "true"),
        )
    )

    assert backend._command("mineru", Path("in.pdf"), Path("out")) == [
        "mineru",
        "-p",
        "in.pdf",
        "-o",
        "out",
        "-b",
        "hybrid-http-client",
        "--effort",
        "high",
        "--method",
        "ocr",
        "--lang",
        "ch",
        "--api-url",
        "http://127.0.0.1:8000",
        "--url",
        "http://127.0.0.1:30000",
        "--debug",
        "true",
    ]


@pytest.mark.parametrize(
    ("overrides", "match"),
    [
        ({"timeout_seconds": 0}, "timeout_seconds must be greater than 0"),
        ({"method": "invalid"}, "method must be one of"),
        ({"effort": "invalid"}, "effort must be one of"),
    ],
)
def test_mineru_options_reject_invalid_cli_controls(overrides, match):
    with pytest.raises(ValueError, match=match):
        MinerUBackendOptions(**overrides)


@pytest.mark.whitebox
def test_paddleocr_pipeline_options_are_sparse_and_advanced():
    backend = PaddleOcrBackend(
        PaddleOcrBackendOptions(
            lang="en",
            device="cpu",
            cpu_threads=2,
            use_table_recognition=False,
            markdown_ignore_labels=("image", "footer"),
        )
    )

    assert backend._pipeline_options() == {
        "lang": "en",
        "device": "cpu",
        "cpu_threads": 2,
        "use_table_recognition": False,
        "markdown_ignore_labels": ["image", "footer"],
    }


class _FailingPipeline:
    def __init__(self, **_options: Any) -> None:
        raise RuntimeError("missing model weights")


class _WorkingPipeline:
    def __init__(self, **_options: Any) -> None:
        pass

    def predict(self, input: str) -> list[dict[str, str]]:
        return [{"markdown": f"parsed {input}"}]

    def concatenate_markdown_pages(self, pages: list[str]) -> str:
        return "\n".join(pages)


@pytest.mark.whitebox
def test_paddleocr_falls_back_when_pipeline_construction_raises():
    fake_module = SimpleNamespace(
        PPStructureV3=_FailingPipeline,
        PaddleOCRVL=_WorkingPipeline,
        PPStructure=_WorkingPipeline,
    )
    backend = PaddleOcrBackend()

    result, warnings = backend._predict(fake_module, "doc.pdf")

    assert result == "parsed doc.pdf"
    assert backend._active_pipeline_class == "PaddleOCRVL"
    assert any("PPStructureV3" in warning for warning in warnings)


class _FailingPredictPipeline:
    def __init__(self, **_options: Any) -> None:
        pass

    def predict(self, input: str) -> Any:
        raise RuntimeError("GPU init failure")


@pytest.mark.whitebox
def test_paddleocr_falls_back_when_predict_raises():
    fake_module = SimpleNamespace(
        PPStructureV3=_FailingPredictPipeline,
        PaddleOCRVL=_WorkingPipeline,
        PPStructure=_WorkingPipeline,
    )
    backend = PaddleOcrBackend()

    result, warnings = backend._predict(fake_module, "doc.pdf")

    assert result == "parsed doc.pdf"
    assert backend._active_pipeline_class == "PaddleOCRVL"
    assert any("GPU init failure" in warning for warning in warnings)


@pytest.mark.whitebox
def test_paddleocr_falls_back_to_legacy_api_when_all_pipelines_fail():
    class _LegacyOcr:
        def __init__(self, **_options: Any) -> None:
            pass

        def ocr(self, path: str, cls: bool = True) -> list[str]:
            return [f"legacy {path}"]

    fake_module = SimpleNamespace(
        PPStructureV3=_FailingPipeline,
        PaddleOCRVL=_FailingPipeline,
        PPStructure=_FailingPipeline,
        PaddleOCR=_LegacyOcr,
    )
    backend = PaddleOcrBackend()

    result, warnings = backend._predict(fake_module, "doc.pdf")

    assert result == ["legacy doc.pdf"]
    assert backend._active_pipeline_class == "PaddleOCR"
    assert len(warnings) == 3


@pytest.mark.blackbox
@pytest.mark.parametrize(
    ("parse_input", "expected_parser", "expected_content"),
    [
        (
            ParseInput(content="name,role\nAda,engineer", content_type="text/csv"),
            CsvParser.parser_name,
            "Ada\tengineer",
        ),
        (
            ParseInput(content='{"name": "Ada"}', filename="data.json"),
            JsonParser.parser_name,
            "$.name: Ada",
        ),
        (
            ParseInput(content="# Title\n\nBody", filename="doc.md"),
            MarkdownParser.parser_name,
            "Title\n\nBody",
        ),
        (
            ParseInput(content="print('hello')", filename="app.py"),
            TextParser.parser_name,
            "print('hello')",
        ),
        (
            ParseInput(content="plain text", content_type="text/plain"),
            TextParser.parser_name,
            "plain text",
        ),
        (
            ParseInput(
                content="<html><script>x()</script><p>Hello</p></html>",
                filename="doc.html",
            ),
            HtmlParser.parser_name,
            "Hello",
        ),
    ],
)
def test_builtin_text_parsers_blackbox(parse_input, expected_parser, expected_content):
    document = HarborParser().parse(parse_input)

    assert document.parser_name == expected_parser
    assert expected_content in document.content
    assert document.elements
    assert all(element.content for element in document.elements)
