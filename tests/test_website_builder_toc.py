#!/usr/bin/env python3
"""Tests for website/builder/markdown_toc.py — table-of-contents rendering."""


class TestMarkdownTocMixin:
    """Test table-of-contents rendering from heading markup."""

    def test_returns_empty_string_without_headings(self, markdown_processor):
        assert markdown_processor.render_toc("<p>No headings at all.</p>") == ""

    def test_builds_nested_lists_and_closes_them(self, markdown_processor):
        html = (
            '<h1 id="top">Top</h1>'
            '<h2 id="mid">Mid</h2>'
            '<h3 id="leaf">Leaf</h3>'
            '<h1 id="second">Second</h1>'
        )

        toc = markdown_processor.render_toc(html)

        assert toc.startswith('<div class="toc"><h3>Table of Contents</h3>')
        assert toc.endswith("</div>")
        assert toc.count("<ul>") == toc.count("</ul>")
        assert 'href="#leaf"' in toc
        assert 'href="#second"' in toc

    def test_sibling_headings_do_not_count_as_children(self, markdown_processor):
        html = '<h2 id="a">A</h2><h2 id="b">B</h2>'

        toc = markdown_processor.render_toc(html)

        assert 'href="#a"' in toc
        assert 'href="#b"' in toc

    def test_level_three_leaf_gets_the_default_icon(self, markdown_processor):
        html = '<h1 id="top">Top</h1><h2 id="mid">Mid</h2><h3 id="leaf">Leaf</h3>'

        toc = markdown_processor.render_toc(html)

        assert "toc-icon" in toc
        assert "<svg" in toc

    def test_numbered_level_three_heading_skips_the_default_icon(self, markdown_processor):
        html = '<h1 id="top">Top</h1><h2 id="mid">Mid</h2><h3 id="step">1. Step</h3>'

        toc = markdown_processor.render_toc(html)

        assert "<svg" not in toc
        assert "1. Step" in toc

    def test_inline_image_is_reused_as_the_icon(self, markdown_processor):
        html = '<h2 id="pic"><img src="a.png" alt="Alpha" width="99" height="99"></h2>'

        toc = markdown_processor.render_toc(html)

        assert "<img" in toc
        assert 'class="toc-icon"' in toc
        assert 'width="99"' not in toc
        assert "Alpha" in toc

    def test_image_class_attribute_is_extended(self, markdown_processor):
        html = '<h2 id="pic"><img class="logo" src="a.png" alt="Alpha">Title</h2>'

        toc = markdown_processor.render_toc(html)

        assert 'class="logo toc-icon"' in toc

    def test_single_quoted_image_class_is_extended(self, markdown_processor):
        html = "<h2 id=\"pic\"><img class='logo' src='a.png' alt='Alpha'>Title</h2>"

        toc = markdown_processor.render_toc(html)

        assert "class='logo toc-icon'" in toc

    def test_unsafe_image_attributes_are_stripped(self, markdown_processor):
        html = (
            '<h2 id="pic"><img src="javascript: alert(1)" onerror="steal()" '
            'style="width:900px" alt="Alpha">Title</h2>'
        )

        toc = markdown_processor.render_toc(html)

        assert "onerror" not in toc
        assert "javascript:" not in toc
        assert "width:900px" not in toc

    def test_heading_id_is_used_when_no_text_or_alt(self, markdown_processor):
        html = '<h2 id="fallback-id"><img src="a.png"></h2>'

        toc = markdown_processor.render_toc(html)

        assert "fallback-id" in toc

    def test_entities_in_heading_text_are_escaped_once(self, markdown_processor):
        html = '<h2 id="amp">Tom &amp; Jerry</h2>'

        toc = markdown_processor.render_toc(html)

        assert "Tom &amp; Jerry" in toc
        assert "&amp;amp;" not in toc
