"""White-box unit tests for HTML-to-Markdown rendering with table structure."""

from __future__ import annotations

import pytest

from harborrag_adapters.parsers.common.html_markdown import (
    MARKDOWN_ENGINE,
    html_to_markdown,
    html_to_markdown_with_engine,
)
from harborrag_adapters.parsers.common.markdown_tables import (
    MAX_TABLE_SPAN,
    TableCell,
    markdown_table,
)

pytestmark = [pytest.mark.unit, pytest.mark.whitebox]


def test_table_keeps_its_rows_and_columns():
    html = """
    <table>
      <tr><th>Service</th><th>CPU</th></tr>
      <tr><td>api</td><td>4</td></tr>
      <tr><td>worker</td><td>8</td></tr>
    </table>
    """

    output = html_to_markdown(html)

    assert output.splitlines() == [
        "| Service | CPU |",
        "| --- | --- |",
        "| api | 4 |",
        "| worker | 8 |",
    ]


def test_headerless_table_gets_positional_headers_and_keeps_every_row():
    html = "<table><tr><td>a</td><td>b</td></tr><tr><td>c</td><td>d</td></tr></table>"

    output = html_to_markdown(html)

    assert output.splitlines() == [
        "| Column 1 | Column 2 |",
        "| --- | --- |",
        "| a | b |",
        "| c | d |",
    ]


def test_spanned_cells_keep_the_grid_rectangular():
    html = """
    <table>
      <tr><th>Env</th><th>Region</th></tr>
      <tr><td rowspan="2">prod</td><td>apac</td></tr>
      <tr><td>emea</td></tr>
    </table>
    """

    output = html_to_markdown(html)

    assert output.splitlines() == [
        "| Env | Region |",
        "| --- | --- |",
        "| prod (spans 2 rows) | apac |",
        "|  | emea |",
    ]


def test_cell_text_cannot_break_out_of_its_cell():
    html = "<table><tr><td>a | b<br>second</td><td>&lt;br&gt;</td></tr></table>"

    output = html_to_markdown(html)

    assert "| a \\| b<br>second | &lt;br&gt; |" in output


def test_headings_lists_and_links_render_as_markdown():
    html = """
    <h2>Rollout</h2>
    <p>See the <a href="https://example.invalid/plan">plan</a>.</p>
    <ul><li>stage one</li><li>stage two</li></ul>
    """

    output = html_to_markdown(html)

    assert "## Rollout" in output
    assert "[plan](https://example.invalid/plan)" in output
    assert "- stage one" in output
    assert "- stage two" in output


def test_drop_selectors_remove_provider_chrome():
    html = '<p>Body</p><span class="aui-icon">icon text</span>'

    assert "icon text" not in html_to_markdown(html, drop_selectors=(".aui-icon",))
    assert "icon text" in html_to_markdown(html)


def test_rendering_reports_the_backend_that_produced_it():
    _, engine = html_to_markdown_with_engine("<p>hi</p>")

    assert engine == MARKDOWN_ENGINE


def test_markdown_table_of_no_rows_renders_nothing():
    assert markdown_table([]) == ""
    assert markdown_table([[TableCell(text="only")]]).splitlines() == [
        "| Column 1 |",
        "| --- |",
        "| only |",
    ]


def test_an_oversized_span_is_clamped_instead_of_allocated():
    output = html_to_markdown('<table><tr><td colspan="1000000000">x</td></tr></table>')

    header, _, row = output.splitlines()
    assert header.count("Column") == MAX_TABLE_SPAN
    assert row.startswith(f"| x (spans {MAX_TABLE_SPAN} columns) |")


def test_a_table_too_large_to_lay_out_keeps_its_text_as_plain_rows():
    wide = [TableCell(text=f"h{index}", column_span=MAX_TABLE_SPAN) for index in range(10)]
    tall = [[TableCell(text=f"r{index}")] for index in range(100)]

    output = markdown_table([wide, *tall])

    assert "|" not in output
    assert output.splitlines()[0] == " ".join(f"h{index}" for index in range(10))
    assert output.splitlines()[-1] == "r99"
