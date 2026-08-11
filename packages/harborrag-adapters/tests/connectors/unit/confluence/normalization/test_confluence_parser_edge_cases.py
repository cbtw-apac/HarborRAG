from __future__ import annotations

from unittest.mock import patch

import pytest
from confluence_fixtures import page_input, walk

from harborrag_adapters.connectors.confluence.normalization import (
    ConfluenceMacroHandlerRegistry,
    ConfluencePageInput,
    ConfluencePageNormalizer,
    UnsupportedConfluenceBodyError,
)
from harborrag_adapters.connectors.confluence.normalization.adf import AdfDocumentParser
from harborrag_adapters.connectors.confluence.normalization.errors import (
    ConfluenceNormalizationError,
)
from harborrag_adapters.connectors.confluence.normalization.macros import (
    default_macro_handlers,
)
from harborrag_adapters.connectors.confluence.normalization.markup import (
    ConfluenceMarkupParser,
    _TreeParser,
)
from harborrag_adapters.connectors.confluence.normalization.nodes import ConfluenceNode
from harborrag_core.domain import DocumentBlockKind

pytestmark = [pytest.mark.unit, pytest.mark.blackbox]


def test_api_payload_selection_keeps_only_safe_structured_fields():
    payload = {
        "id": "42",
        "title": "Guide",
        "space": {"id": "space-1", "key": "ENG"},
        "version": {"number": 7},
        "ancestors": [
            {"id": "1", "title": "Engineering"},
            "ignored",
            {"id": "", "title": "Incomplete"},
        ],
        "metadata": {
            "labels": {
                "results": [{"name": "runbook"}, {"name": ""}, "ignored"],
            }
        },
        "body": {
            "atlas_doc_format": {"value": {"type": "doc", "version": 1, "content": []}},
            "storage": {"value": "<p>storage</p>"},
            "export_view": {"value": ""},
            "view": {"value": "<p>rendered</p>"},
        },
        "authorization": "must-not-survive",
    }

    selected = ConfluencePageInput.from_api_payload(
        payload,
        source_url="https://example.test/wiki/pages/42",
    )
    document = ConfluencePageNormalizer().normalize_payload(
        payload,
        source_url="https://example.test/wiki/pages/42",
    )

    assert selected.page_version == "7"
    assert selected.ancestors == (("1", "Engineering"),)
    assert selected.labels == ("runbook",)
    assert selected.storage == "<p>storage</p>"
    assert selected.rendered_html == "<p>rendered</p>"
    assert "authorization" not in document.provenance.extra


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"page_id": " "}, "identity"),
        ({"document_id": ""}, "document_id"),
        ({"document_version_id": ""}, "document_version_id"),
        ({"ancestors": (("", "Title"),)}, "ancestors"),
        ({"labels": ("",)}, "labels"),
    ],
)
def test_page_input_rejects_blank_required_structural_values(changes, message):
    with pytest.raises(ValueError, match=message):
        page_input(**changes)


def test_adf_parser_covers_lists_breaks_cards_and_unsupported_nodes():
    root = AdfDocumentParser().parse(
        {
            "type": "doc",
            "content": [
                {
                    "type": "orderedList",
                    "content": [
                        {
                            "type": "listItem",
                            "content": [
                                {
                                    "type": "paragraph",
                                    "content": [
                                        {"type": "text", "text": "first"},
                                        {"type": "hardBreak"},
                                        {"type": "inlineCard", "attrs": {"url": "/pages/99"}},
                                    ],
                                }
                            ],
                        }
                    ],
                },
                {"type": "heading", "attrs": {"level": True}},
                {"type": "heading", "attrs": {"level": "invalid"}},
                {"type": "rule"},
                {"type": "vendorNode", "content": "not-a-node-sequence"},
            ],
        }
    )

    assert root.children[0].attributes["ordered"] is True
    assert root.children[0].visible_text() == "first"
    assert root.children[0].children[0].children[0].children[2].attributes["href"] == "/pages/99"
    assert root.children[1].attributes["level"] == 1
    assert root.children[2].attributes["level"] == 1
    assert [node.kind for node in root.children[-2:]] == ["unsupported", "unsupported"]

    with pytest.raises(ConfluenceNormalizationError, match="valid JSON"):
        AdfDocumentParser().parse("{broken")
    with pytest.raises(ConfluenceNormalizationError, match="root"):
        AdfDocumentParser().parse({"type": "paragraph"})


def test_adf_parser_handles_deeply_nested_documents_without_recursion_error():
    depth = 5000
    node: dict = {"type": "text", "text": "leaf"}
    for _ in range(depth):
        node = {"type": "panel", "content": [node]}
    document = {"type": "doc", "content": [node]}

    root = AdfDocumentParser().parse(document)

    current = root.children[0]
    for _ in range(depth - 1):
        assert current.kind == "panel"
        current = current.children[0]
    assert current.children[0].text == "leaf"


def test_storage_markup_preserves_structural_macros_references_and_default_titles():
    storage = """
    bare text
    <h2>Storage Heading</h2>
    <ol><li>First</li></ol>
    <pre>print(1)</pre>
    <blockquote>Quoted</blockquote>
    <details><summary>Summary</summary><p>Collapsed body</p></details>
    <div class="confluence-information-macro panel" title="Caution"><p>Panel body</p></div>
    <ac:structured-macro ac:name="page-properties" ac:macro-id="macro-1">
      <ac:parameter ac:name="title">Properties</ac:parameter>
      <ac:parameter ac:name="token">secret</ac:parameter>
      <ac:rich-text-body><p>Visible macro body</p></ac:rich-text-body>
    </ac:structured-macro>
    <p><a href="javascript:alert(1)">Unsafe</a></p>
    <ri:page ri:content-id="99" ri:content-title="Child"/>
    <ri:attachment ri:content-id="attachment-1" ri:filename="limits.csv"/>
    <img src="image.png" alt="chart.png"/>
    """

    document = ConfluencePageNormalizer().normalize(
        page_input(adf=None, storage=storage, rendered_html="<p>unused</p>")
    )
    blocks = tuple(walk(document.blocks[0]))
    by_kind = {
        kind: [block for block in blocks if block.kind == kind] for kind in DocumentBlockKind
    }

    assert document.body_representation == "storage"
    assert by_kind[DocumentBlockKind.LIST][0].attributes["ordered"] is True
    assert by_kind[DocumentBlockKind.EXPAND][0].title == "Details"
    assert by_kind[DocumentBlockKind.PANEL][0].title == "Caution"
    macro = by_kind[DocumentBlockKind.MACRO][0]
    assert macro.attributes["parameters"] == {"title": "Properties"}
    assert macro.attributes["emits_table"] is True
    unsafe = next(
        block for block in by_kind[DocumentBlockKind.LINK_REFERENCE] if block.text == "Unsafe"
    )
    assert unsafe.attributes["target_url"] == ""
    assert any(
        block.attributes.get("target_page_id") == "99"
        for block in by_kind[DocumentBlockKind.LINK_REFERENCE]
    )
    assert any(
        block.attributes.get("attachment_id") == "attachment-1"
        for block in by_kind[DocumentBlockKind.MEDIA_REFERENCE]
    )
    assert "Visible macro body" in "\n".join(element.content for element in document.content)


def test_storage_include_in_layout_table_becomes_reference_inside_local_tab():
    storage = """
    <ac:structured-macro ac:name="localtabgroup" ac:macro-id="tabs-1">
      <ac:rich-text-body>
        <ac:structured-macro ac:name="localtab" ac:macro-id="tab-1">
          <ac:parameter ac:name="title">Project Charter</ac:parameter>
          <ac:rich-text-body>
            <table id="layout-table"><tbody><tr><td>
              <ac:structured-macro ac:name="include" ac:macro-id="include-1">
                <ac:parameter ac:name="">
                  <ac:link>
                    <ri:page ri:content-id="44" ri:content-title="Charter"
                             ri:space-key="ARCH" />
                  </ac:link>
                </ac:parameter>
              </ac:structured-macro>
            </td></tr></tbody></table>
          </ac:rich-text-body>
        </ac:structured-macro>
      </ac:rich-text-body>
    </ac:structured-macro>
    """

    document = ConfluencePageNormalizer().normalize(
        page_input(adf=None, storage=storage, rendered_html=None)
    )
    blocks = tuple(walk(document.blocks[0]))
    tab_set = next(block for block in blocks if block.kind == DocumentBlockKind.TAB_SET)
    tab = next(block for block in blocks if block.kind == DocumentBlockKind.TAB)
    include = next(block for block in blocks if block.kind == DocumentBlockKind.LINK_REFERENCE)

    assert tab_set.attributes["macro_key"] == "localtabgroup"
    assert tab.title == "Project Charter"
    assert include.text == "Charter"
    assert include.tab_path == ("Project Charter",)
    assert include.attributes["reference_kind"] == "include"
    assert include.attributes["target_page_id"] == "44"
    assert include.attributes["target_title"] == "Charter"
    assert include.attributes["target_space_key"] == "ARCH"
    assert document.table_artifacts == ()
    assert [(relation.predicate, relation.target_id) for relation in document.relations] == [
        ("includes", "confluence://ARCH/44")
    ]
    assert document.warnings == ()


def test_title_only_include_is_preserved_without_network_identity_or_relation():
    storage = """
    <ac:structured-macro ac:name="include" ac:macro-id="include-title-only">
      <ac:parameter ac:name="">
        <ac:link><ri:page ri:content-title="Project Organization" /></ac:link>
      </ac:parameter>
    </ac:structured-macro>
    """

    document = ConfluencePageNormalizer().normalize(
        page_input(adf=None, storage=storage, rendered_html=None)
    )
    include = next(
        block
        for block in walk(document.blocks[0])
        if block.kind == DocumentBlockKind.LINK_REFERENCE
    )

    assert include.text == "Project Organization"
    assert include.attributes["target_page_id"] == ""
    assert include.attributes["target_title"] == "Project Organization"
    assert include.attributes["target_space_key"] == "ENG"
    assert document.relations == []


def test_unknown_adf_blocks_and_markup_failures_remain_explicit():
    document = ConfluencePageNormalizer().normalize(
        page_input([{"type": "rule"}, {"type": "mystery", "content": []}])
    )

    unsupported = [
        block for block in walk(document.blocks[0]) if block.kind == DocumentBlockKind.UNSUPPORTED
    ]
    assert len(unsupported) == 2
    assert document.warnings == ("unsupported Confluence block preserved: unsupported",)

    with pytest.raises(ConfluenceNormalizationError, match="empty"):
        ConfluenceMarkupParser().parse("")
    with (
        patch.object(_TreeParser, "feed", side_effect=ValueError("bad")),
        pytest.raises(ConfluenceNormalizationError, match="malformed"),
    ):
        ConfluenceMarkupParser().parse("<p>bad</p>")
    with pytest.raises(UnsupportedConfluenceBodyError, match="no supported"):
        ConfluencePageNormalizer().normalize(page_input(adf=None, storage=None, rendered_html=None))


def test_markup_parser_handles_tolerant_tree_and_parameter_edges():
    root = ConfluenceMarkupParser().parse(
        """
        <div/>
        <div><span>nested</div>
        </unmatched>
        <ac:structured-macro ac:name="status">
          <ac:parameter ac:name="title">Ready</ac:parameter>
          <ac:parameter ac:name="empty"></ac:parameter>
          <ac:parameter ac:name="colour">Green</ac:parameter>
        </ac:structured-macro>
        """
    )

    macro = next(child for child in root.children if child.kind == "macro")
    assert macro.attributes["parameters"] == {"title": "Ready", "colour": "Green"}
    assert "nested" in root.visible_text()


@pytest.mark.parametrize(
    "changes",
    [
        {"kind": " "},
        {"source_id": ""},
    ],
)
def test_confluence_node_requires_stable_structural_identity(changes):
    values = {"kind": "paragraph", "source_id": "node-1"}
    values.update(changes)
    with pytest.raises(ValueError, match="non-empty"):
        ConfluenceNode(kind=values["kind"], source_id=values["source_id"])


def test_macro_registry_rejects_duplicate_aliases_and_exposes_known_capabilities():
    handlers = default_macro_handlers()
    registry = ConfluenceMacroHandlerRegistry()

    assert registry.resolve(" PAGE_PROPERTIES ").emits_table is True
    assert registry.resolve("localtabgroup").emits_container is True
    assert registry.resolve("localtab").emits_container is True
    assert registry.resolve("include").needs_rendered_fallback is True
    assert registry.resolve("include").emits_visible_content is False
    assert registry.resolve("unknown").needs_rendered_fallback is True
    with pytest.raises(ValueError, match="duplicate"):
        ConfluenceMacroHandlerRegistry((*handlers, handlers[0]))
