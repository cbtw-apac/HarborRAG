from __future__ import annotations

import io
import zipfile

import pytest
from harbor_test_builders import build_epub_bytes

from harborrag_adapters._shared.text.html_markdown import html_to_markdown
from harborrag_adapters._shared.text.markdown_tables import TableCell, markdown_table
from harborrag_adapters.connectors.sharepoint.pages import render_page_html
from harborrag_adapters.parsers.compat import EpubParser, HtmlParser
from harborrag_core.domain.parser import ParseInput

pytestmark = pytest.mark.unit

_TABLE_HTML = (
    "<table>"
    "<tr><th>Check Area</th><th>Expected Result</th></tr>"
    "<tr><td>Upload</td><td>Completed successfully</td></tr>"
    "</table>"
)
_TABLE_MARKDOWN = (
    "| Check Area | Expected Result |\n| --- | --- |\n| Upload | Completed successfully |"
)


def test_html_table_renders_as_markdown_table_between_its_paragraphs() -> None:
    html = f"<html><body><p>Intro</p>{_TABLE_HTML}<p>Closing note.</p></body></html>"

    parsed = HtmlParser().parse(ParseInput(content=html.encode(), filename="page.html"))

    assert parsed.content == f"Intro\n\n{_TABLE_MARKDOWN}\n\nClosing note."


def test_table_directly_inside_a_list_item_is_kept_under_it() -> None:
    markdown = html_to_markdown(f"<ul><li>Results{_TABLE_HTML}</li></ul>")

    assert markdown == "- Results\n  " + _TABLE_MARKDOWN.replace("\n", "\n  ")


def test_table_inside_a_blockquote_is_kept_in_the_quote() -> None:
    markdown = html_to_markdown(f"<blockquote><p>Quoted results</p>{_TABLE_HTML}</blockquote>")

    assert markdown == "> Quoted results\n>\n> " + _TABLE_MARKDOWN.replace("\n", "\n> ")


def test_row_span_taller_than_the_span_limit_keeps_later_cells_in_their_column() -> None:
    rows = [
        [TableCell("Owner", header=True), TableCell("Step", header=True)],
        [TableCell("Alex Example", row_span=101), TableCell("step 1")],
        *([TableCell(f"step {index}")] for index in range(2, 102)),
        [TableCell("Sam Sample"), TableCell("step 102")],
    ]

    lines = markdown_table(rows).splitlines()

    assert lines[2] == "| Alex Example (spans 101 rows) | step 1 |"
    assert lines[102] == "|  | step 101 |"
    assert lines[103] == "| Sam Sample | step 102 |"


def test_html_inline_data_image_keeps_only_its_alt_text() -> None:
    html = (
        '<p>Intro</p><p><img alt="logo" src="data:image/png;base64,iVBORw0KGgoAAAANSUhEUg=="></p>'
        '<p><img alt="chart" src="https://example.test/chart.png"></p>'
    )

    parsed = HtmlParser().parse(ParseInput(content=html.encode(), filename="page.html"))

    assert parsed.content == "Intro\n\nlogo\n\n![chart](https://example.test/chart.png)"


def test_sharepoint_page_table_reaches_the_parsed_text_as_a_markdown_table() -> None:
    page = {
        "title": "Release checks",
        "canvasLayout": {
            "horizontalSections": [
                {
                    "columns": [
                        {
                            "webparts": [
                                {
                                    "@odata.type": "#microsoft.graph.textWebPart",
                                    "innerHtml": f"<p>Intro</p>{_TABLE_HTML}",
                                }
                            ]
                        }
                    ]
                }
            ]
        },
    }
    html, _ = render_page_html(page)

    parsed = HtmlParser().parse(
        ParseInput(content=html.encode(), filename="release.aspx", content_type="text/html")
    )

    assert parsed.content == f"# Release checks\n\nIntro\n\n{_TABLE_MARKDOWN}"


def test_epub_chapter_table_renders_as_markdown_table() -> None:
    chapter = f"<html><body><p>Intro</p>{_TABLE_HTML}</body></html>".encode()
    package = _replace_chapters(build_epub_bytes(["placeholder"]), chapter)

    parsed = EpubParser().parse(ParseInput(content=package, filename="book.epub"))

    assert _TABLE_MARKDOWN in parsed.content
    assert parsed.content.startswith("Intro\n\n| Check Area |")


def _replace_chapters(package: bytes, chapter: bytes) -> bytes:
    output = io.BytesIO()
    with (
        zipfile.ZipFile(io.BytesIO(package)) as source,
        zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as target,
    ):
        for item in source.infolist():
            is_chapter = item.filename.endswith((".xhtml", ".html")) and "nav" not in item.filename
            target.writestr(item, chapter if is_chapter else source.read(item))
    return output.getvalue()
