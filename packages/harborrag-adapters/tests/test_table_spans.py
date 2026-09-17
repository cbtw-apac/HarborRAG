import textwrap

from harborrag_adapters.connectors.jira.content import _adf_table_to_markdown
from harborrag_adapters.connectors.jira.html_to_markdown import html_to_markdown


def test_html_rowspan_and_colspan():
    html = textwrap.dedent(
        """
        <table>
          <tr><th>H1</th><th>H2</th></tr>
          <tr><td rowspan="2">A</td><td>B</td></tr>
          <tr><td>C</td></tr>
        </table>
        """
    )
    md = html_to_markdown(html)
    assert "| H1 | H2 |" in md
    assert "| --- | --- |" in md
    # first data row contains A and B
    assert "| A | B |" in md
    # second data row should contain C in one of the columns
    assert "| C |" in md


def test_adf_rowspan_and_colspan():
    adf = {
        "type": "table",
        "content": [
            {
                "type": "tableRow",
                "content": [
                    {"type": "tableHeader", "content": [{"type": "text", "text": "H1"}]},
                    {"type": "tableHeader", "content": [{"type": "text", "text": "H2"}]},
                ],
            },
            {
                "type": "tableRow",
                "content": [
                    {
                        "type": "tableCell",
                        "attrs": {"rowspan": 2},
                        "content": [{"type": "text", "text": "A"}],
                    },
                    {"type": "tableCell", "content": [{"type": "text", "text": "B"}]},
                ],
            },
            {
                "type": "tableRow",
                "content": [
                    {"type": "tableCell", "content": [{"type": "text", "text": "C"}]},
                ],
            },
        ],
    }
    md = _adf_table_to_markdown(adf)
    assert "| H1 | H2 |" in md
    assert "| --- | --- |" in md
    assert "| A | B |" in md
    assert "| C |" in md
