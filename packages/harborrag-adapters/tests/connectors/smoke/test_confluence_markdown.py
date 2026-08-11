from __future__ import annotations

import pytest
from bootstrap.confluence_markdown import confluence_html_to_markdown

pytestmark = [pytest.mark.unit, pytest.mark.whitebox]


def test_renders_headings_links_emphasis_lists_and_code() -> None:
    html = """
    <h2>Overview</h2>
    <p>Read <a href="/wiki/page"><strong>the guide</strong></a>.</p>
    <ol><li>First</li><li>Run <code>pytest</code></li></ol>
    <pre>line 1\nline 2</pre>
    """

    markdown = confluence_html_to_markdown(html)

    assert "## Overview" in markdown
    assert "[**the guide**](/wiki/page)" in markdown
    assert "1. First" in markdown
    assert "2. Run `pytest`" in markdown
    assert "```text\nline 1\nline 2\n```" in markdown


def test_renders_row_and_column_spans_as_a_readable_logical_grid() -> None:
    html = """
    <table>
      <tr><th rowspan="2">Service</th><th colspan="2">Limits</th></tr>
      <tr><th>CPU</th><th>RAM</th></tr>
      <tr><td>api</td><td>4</td><td>8 GB</td></tr>
    </table>
    """

    markdown = confluence_html_to_markdown(html)

    assert "| Service (spans 2 rows) | Limits (spans 2 columns) |  |" in markdown
    assert "|  | CPU | RAM |" in markdown
    assert "| api | 4 | 8 GB |" in markdown
    assert "<table" not in markdown


def test_escapes_literal_angle_brackets_inside_table_cells() -> None:
    markdown = confluence_html_to_markdown(
        "<table><tr><td>&lt;Project Space Key&gt;</td></tr></table>"
    )

    assert "| &lt;Project Space Key&gt; |" in markdown


def test_renders_confluence_tab_wrapper_content_after_navigation_item() -> None:
    html = """
    <ul style="list-style-type: none">
      <li><a>Project Charter</a></li>
      <div><p>Included introduction</p><table><tr><td>Value</td></tr></table></div>
    </ul>
    """

    markdown = confluence_html_to_markdown(html)

    assert "- Project Charter" in markdown
    assert "Included introduction" in markdown
    assert "| Column 1 |" in markdown
    assert "| Value |" in markdown


def test_nested_table_is_rendered_separately_from_one_cell_layout_wrapper() -> None:
    html = """
    <table><tr><td><div>
      <table><tr><th>Service</th><th>CPU</th></tr><tr><td>api</td><td>4</td></tr></table>
    </div><p>Source notes</p></td></tr></table>
    """

    markdown = confluence_html_to_markdown(html)

    assert "| Source notes |" in markdown
    assert "**Nested table 1**" in markdown
    assert "| Service | CPU |" in markdown
    assert "| api | 4 |" in markdown
    assert "ServiceCPU" not in markdown


def test_drops_script_style_buttons_and_decorative_icons() -> None:
    html = """
    <style>.hidden { color: red; }</style>
    <script>alert('no')</script>
    <button>Create</button>
    <span class="aui-icon">icon</span>
    <p>Visible</p>
    """

    markdown = confluence_html_to_markdown(html)

    assert markdown == "Visible"
