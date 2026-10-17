"""Page text keeps inline content in reading order, once, in the block that holds it.

Chunking reads ``Document.content``, so these tests assert on the elements a page
produces rather than on the intermediate block tree.
"""

from __future__ import annotations

import pytest
from confluence_fixtures import page_input, text, walk

from harborrag_adapters.connectors.confluence.normalization import ConfluencePageNormalizer

pytestmark = [pytest.mark.unit, pytest.mark.blackbox]


def _storage_content(storage: str) -> list[tuple[str, str]]:
    document = ConfluencePageNormalizer().normalize(
        page_input(adf=None, storage=storage, rendered_html=None)
    )
    return [(element.type, element.content) for element in document.content]


def _adf_content(*content: dict) -> list[tuple[str, str]]:
    document = ConfluencePageNormalizer().normalize(page_input(list(content)))
    return [(element.type, element.content) for element in document.content]


def _marked(value: str, mark: str) -> dict:
    return {**text(value), "marks": [{"type": mark}]}


def test_storage_paragraph_keeps_text_around_inline_elements_in_order():
    content = _storage_content(
        '<p>See <a href="https://example.com/docs">docs</a> for <strong>more</strong> details.</p>'
    )

    assert content == [("paragraph", "See docs for more details.")]


def test_storage_inline_code_stays_inside_its_list_item():
    content = _storage_content("<ul><li>Install <code>pip</code> first</li></ul>")

    assert content == [("list_item", "Install pip first")]


def test_storage_table_cell_keeps_inline_text_in_order():
    document = ConfluencePageNormalizer().normalize(
        page_input(
            adf=None,
            storage=(
                "<table><tbody><tr><th>Amount</th><th>Note</th></tr>"
                "<tr><td>1<strong>000</strong></td><td>a <strong>b</strong> c</td></tr>"
                "</tbody></table>"
            ),
            rendered_html=None,
        )
    )

    cells = {
        (cell.row_index, cell.column_index): cell for cell in document.table_artifacts[0].cells
    }
    assert cells[(1, 0)].text == "1000"
    assert cells[(1, 1)].text == "a b c"


@pytest.mark.parametrize("macro", ["code", "noformat"])
def test_storage_code_macro_body_becomes_a_code_element(macro: str):
    content = _storage_content(
        f'<ac:structured-macro ac:name="{macro}">'
        '<ac:parameter ac:name="language">python</ac:parameter>'
        "<ac:plain-text-body><![CDATA[def f():\n    return 1]]></ac:plain-text-body>"
        "</ac:structured-macro>"
    )

    assert content == [("code", "def f():\n    return 1")]


def test_storage_link_body_in_cdata_is_kept_in_the_sentence():
    content = _storage_content(
        '<p>Go to <ac:link><ri:page ri:content-title="Other"/>'
        "<ac:plain-text-link-body><![CDATA[the other page]]></ac:plain-text-link-body>"
        "</ac:link> now</p>"
    )

    assert content == [("paragraph", "Go to the other page now")]


def test_storage_line_break_separates_lines():
    assert _storage_content("<p>line1<br/>line2</p>") == [("paragraph", "line1\nline2")]


def test_storage_preformatted_code_is_emitted_once_with_indentation():
    content = _storage_content("<pre><code>x = 1\n    y = 2</code></pre>")

    assert content == [("code", "x = 1\n    y = 2")]


def test_adf_marks_do_not_split_a_paragraph():
    content = _adf_content(
        {
            "type": "paragraph",
            "content": [
                text("The "),
                _marked("quick", "strong"),
                text(" brown un"),
                _marked("believ", "em"),
                text("able"),
            ],
        }
    )

    assert content == [("paragraph", "The quick brown unbelievable")]


def test_adf_heading_with_code_mark_keeps_one_line_title():
    content = _adf_content(
        {
            "type": "heading",
            "attrs": {"level": 2},
            "content": [text("Release "), _marked("2.0", "code")],
        }
    )

    assert content == [("heading", "Release 2.0")]


def test_adf_code_block_keeps_first_line_indentation():
    content = _adf_content({"type": "codeBlock", "content": [text("    indented()\n    more()")]})

    assert content == [("code", "    indented()\n    more()")]


def test_adf_inline_nodes_render_the_text_held_in_their_attributes():
    content = _adf_content(
        {
            "type": "paragraph",
            "content": [
                text("Owner "),
                {"type": "mention", "attrs": {"id": "user-1", "text": "@Jane"}},
                text(", status "),
                {"type": "status", "attrs": {"text": "DONE", "color": "green"}},
                text(", due "),
                {"type": "date", "attrs": {"timestamp": "1767225600000"}},
                text(" "),
                {"type": "emoji", "attrs": {"shortName": ":tada:", "text": "🎉"}},
            ],
        }
    )

    assert content == [("paragraph", "Owner @Jane, status DONE, due 2026-01-01 🎉")]


def test_adf_task_and_decision_items_become_list_items():
    content = _adf_content(
        {
            "type": "taskList",
            "attrs": {"localId": "tasks"},
            "content": [
                {
                    "type": "taskItem",
                    "attrs": {"localId": "task-1", "state": "TODO"},
                    "content": [text("Ship v2")],
                }
            ],
        },
        {
            "type": "decisionList",
            "attrs": {"localId": "decisions"},
            "content": [
                {
                    "type": "decisionItem",
                    "attrs": {"localId": "decision-1", "state": "DECIDED"},
                    "content": [text("Use Postgres")],
                }
            ],
        },
    )

    assert content == [("list_item", "Ship v2"), ("list_item", "Use Postgres")]


def test_adf_legacy_macro_parameters_are_unwrapped_from_macro_params():
    # Cloud stores a legacy macro's parameters as `macroParams.<name>.value`.
    panel = {
        "type": "bodiedExtension",
        "attrs": {
            "extensionType": "com.atlassian.confluence.macro.core",
            "extensionKey": "panel",
            "parameters": {
                "macroParams": {"title": {"value": "Important"}},
                "macroMetadata": {"schemaVersion": {"value": "1"}},
            },
        },
        "content": [{"type": "paragraph", "content": [text("x")]}],
    }

    document = ConfluencePageNormalizer().normalize(page_input([panel]))

    block = next(block for block in walk(document.blocks[0]) if block.kind.value == "panel")
    assert block.title == "Important"
    assert block.attributes["parameters"] == {"title": "Important"}


def test_adf_layout_columns_and_captions_render_in_order_without_warnings():
    document = ConfluencePageNormalizer().normalize(
        page_input(
            [
                {
                    "type": "layoutSection",
                    "content": [
                        {
                            "type": "layoutColumn",
                            "attrs": {"width": 50},
                            "content": [{"type": "paragraph", "content": [text("Left")]}],
                        },
                        {
                            "type": "layoutColumn",
                            "attrs": {"width": 50},
                            "content": [{"type": "paragraph", "content": [text("Right")]}],
                        },
                    ],
                },
                {
                    "type": "mediaSingle",
                    "content": [
                        {"type": "media", "attrs": {"id": "attachment-1", "type": "file"}},
                        {"type": "caption", "content": [text("Figure 1")]},
                    ],
                },
            ]
        )
    )

    assert [(element.type, element.content) for element in document.content] == [
        ("paragraph", "Left"),
        ("paragraph", "Right"),
        ("paragraph", "Figure 1"),
    ]
    assert document.warnings == ()
