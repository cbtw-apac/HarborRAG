from __future__ import annotations

from dataclasses import replace

import pytest
from confluence_fixtures import heading, page_input, table, walk

from harborrag_adapters.connectors.confluence.normalization import (
    ConfluencePageNormalizer,
    TableExtractionError,
)
from harborrag_core.domain import DocumentBlockKind, TableCellType

pytestmark = [pytest.mark.unit, pytest.mark.blackbox]


def test_table_artifact_retains_headers_units_topology_and_location():
    source = table(
        ["Service", "CPU (cores)", "Enabled"],
        [["worker", "2", "true"], ["api", "4", "false"]],
    )
    source["content"][0]["content"].pop()
    source["content"][0]["content"][0]["attrs"] = {"rowspan": 2}
    source["content"][0]["content"][1]["attrs"] = {"colspan": 2}
    source["content"][1]["content"].pop(0)
    page = page_input([heading(1, "Limits"), source])

    document = ConfluencePageNormalizer().normalize(page)
    artifact = document.table_artifacts[0]
    reference = next(
        block
        for block in walk(document.blocks[0])
        if block.kind == DocumentBlockKind.TABLE_REFERENCE
    )

    assert artifact.section_path == ("Limits",)
    assert artifact.header_row_indices == (0,)
    assert artifact.column_names == ("Service", "CPU (cores)", "CPU (cores)")
    assert artifact.units[0].unit == "cores"
    assert artifact.cells[0].row_span == 2
    assert artifact.cells[1].column_span == 2
    assert artifact.logical_grid[1][0].inherited is True
    assert artifact.logical_grid[0][2].inherited is True
    assert reference.attributes["table_id"] == artifact.table_id
    assert reference.text is None
    table_element = next(element for element in document.content if element.type == "table")
    assert "Structured table reference" not in table_element.content
    assert "api" in table_element.content
    assert "\t4\tfalse" in table_element.content
    assert "CPU (cores)" in table_element.content


def test_nested_table_is_separate_and_parent_cell_retains_reference():
    nested = table(["Key", "Value"], [["timeout", "30"]], local_id="nested")
    parent = table(["Name", "Details"], [["worker", ""]], local_id="parent")
    parent["content"][1]["content"][1]["content"].append(nested)

    document = ConfluencePageNormalizer().normalize(page_input([parent]))
    parent_artifact, nested_artifact = document.table_artifacts
    parent_cell = next(cell for cell in parent_artifact.cells if cell.nested_table_ids)

    assert parent_cell.text == ""
    assert parent_cell.nested_table_ids == (nested_artifact.table_id,)
    assert nested_artifact.parent_cell is not None
    assert nested_artifact.parent_cell.table_id == parent_artifact.table_id
    assert nested_artifact.parent_cell.row_index == 1
    assert nested_artifact.parent_cell.column_index == 1
    table_elements = [element for element in document.content if element.type == "table"]
    assert len(table_elements) == 2
    assert "worker" in table_elements[0].content
    assert "timeout" in table_elements[1].content
    assert "30" in table_elements[1].content


def test_table_and_version_identities_are_location_and_content_sensitive():
    duplicate_a = table(["A"], [["same"]], local_id="a")
    duplicate_b = table(["A"], [["same"]], local_id="b")
    original_page = page_input([duplicate_a, duplicate_b])
    updated_page = replace(
        original_page,
        page_version="8",
        adf={
            "type": "doc",
            "version": 1,
            "content": [table(["A"], [["changed"]], local_id="a")],
        },
    )

    first = ConfluencePageNormalizer().normalize(original_page)
    repeated = ConfluencePageNormalizer().normalize(original_page)
    updated = ConfluencePageNormalizer().normalize(updated_page)

    assert first.table_artifacts[0].table_id == repeated.table_artifacts[0].table_id
    assert first.table_artifacts[0].table_version_id == (
        repeated.table_artifacts[0].table_version_id
    )
    assert first.table_artifacts[0].table_id != first.table_artifacts[1].table_id
    assert first.table_artifacts[0].table_id == updated.table_artifacts[0].table_id
    assert first.table_artifacts[0].table_version_id != (
        updated.table_artifacts[0].table_version_id
    )
    assert first.table_artifacts[0].content_hash != updated.table_artifacts[0].content_hash


def test_table_inside_tab_and_expand_inherits_exact_provenance():
    source = table(["Service", "CPU"], [["worker", "2"]])
    page = page_input(
        [
            {
                "type": "bodiedExtension",
                "attrs": {"extensionKey": "tabs", "parameters": {"title": "Environment"}},
                "content": [
                    {
                        "type": "bodiedExtension",
                        "attrs": {"extensionKey": "tab", "parameters": {"title": "Production"}},
                        "content": [
                            {
                                "type": "expand",
                                "attrs": {"title": "Advanced"},
                                "content": [heading(2, "Limits"), source],
                            }
                        ],
                    }
                ],
            }
        ]
    )

    artifact = ConfluencePageNormalizer().normalize(page).table_artifacts[0]

    assert artifact.tab_path == ("Production",)
    assert artifact.section_path == ("Limits",)
    assert artifact.source_locator.source_element_ids == ("table-1",)


def test_storage_table_fallback_preserves_row_and_column_spans():
    storage = """
    <h2>Storage</h2>
    <table id="storage-table">
      <tr><th rowspan="2">Service</th><th colspan="2">Limits</th></tr>
      <tr><th>CPU</th><th>RAM [GB]</th></tr>
      <tr><td>worker</td><td>2</td><td>4</td></tr>
    </table>
    """

    document = ConfluencePageNormalizer().normalize(
        page_input(adf=None, storage=storage, rendered_html=None)
    )
    artifact = document.table_artifacts[0]

    assert document.body_representation == "storage"
    assert artifact.row_count == 3
    assert artifact.column_count == 3
    assert artifact.cells[0].row_span == 2
    assert artifact.cells[1].column_span == 2
    assert artifact.units[0].unit == "GB"


def test_table_without_explicit_headers_uses_stable_names_and_typed_empty_cells():
    source = {
        "type": "table",
        "attrs": {"localId": "headerless"},
        "content": [
            {
                "type": "tableRow",
                "content": [
                    {"type": "tableCell", "content": [paragraph]}
                    for paragraph in (
                        {"type": "paragraph", "content": [{"type": "text", "text": "worker"}]},
                        {"type": "paragraph", "content": [{"type": "text", "text": "2.5"}]},
                        {"type": "paragraph", "content": []},
                    )
                ],
            }
        ],
    }

    artifact = ConfluencePageNormalizer().normalize(page_input([source])).table_artifacts[0]

    assert artifact.header_row_indices == ()
    assert artifact.column_names == ("Column 1", "Column 2", "Column 3")
    assert [cell.cell_type for cell in artifact.cells] == [
        TableCellType.TEXT,
        TableCellType.NUMBER,
        TableCellType.EMPTY,
    ]


def test_table_with_no_extractable_cell_text_still_yields_a_nonempty_element():
    # A layout table whose cells hold only media (no text) renders to pure
    # whitespace; the chunking segmenter drops whitespace-only elements,
    # which would otherwise leave the canonical table artifact with no
    # corresponding chunk and fail projection verification.
    source = {
        "type": "table",
        "attrs": {"localId": "image-only"},
        "content": [
            {
                "type": "tableRow",
                "content": [
                    {
                        "type": "tableCell",
                        "content": [
                            {
                                "type": "mediaSingle",
                                "content": [
                                    {"type": "media", "attrs": {"id": "m1", "type": "file"}}
                                ],
                            }
                        ],
                    }
                    for _ in range(2)
                ],
            }
        ],
    }

    document = ConfluencePageNormalizer().normalize(page_input([source]))
    artifact = document.table_artifacts[0]
    table_element = next(element for element in document.content if element.type == "table")

    assert table_element.content.strip() != ""
    assert str(artifact.row_count) in table_element.content
    assert str(artifact.column_count) in table_element.content


@pytest.mark.parametrize(
    "source",
    [
        {"type": "table", "attrs": {"localId": "no-rows"}, "content": []},
        {
            "type": "table",
            "attrs": {"localId": "no-cells"},
            "content": [{"type": "tableRow", "content": []}],
        },
    ],
)
def test_structurally_empty_tables_raise_domain_specific_errors(source):
    with pytest.raises(TableExtractionError, match="no source (rows|cells)"):
        ConfluencePageNormalizer().normalize(page_input([source]))
