"""Tests for the public origin used by canonical, Open Graph and SEO URLs.

``base_url`` is a path prefix and is deliberately empty for root-served
deployments; the absolute origin is a separate input. These tests pin that
separation so a host change is one flag, not a search for four literals.
"""

import pytest
from builder.constants import DEFAULT_PUBLIC_ORIGIN, resolve_public_origin

pytestmark = [pytest.mark.unit]


def test_site_url_wins_over_base_url():
    assert resolve_public_origin("", "https://docs.example.test/") == "https://docs.example.test"


def test_site_url_wins_even_when_base_url_is_absolute():
    origin = resolve_public_origin("https://old.example.test", "https://new.example.test")
    assert origin == "https://new.example.test"


def test_absolute_base_url_is_still_honoured():
    # Historical behaviour: builds that only pass --base-url keep working.
    assert resolve_public_origin("https://docs.example.test/") == "https://docs.example.test"


def test_empty_inputs_fall_back_to_the_documented_default():
    assert resolve_public_origin("", None) == DEFAULT_PUBLIC_ORIGIN


class TestBuilderUsesPublicOrigin:
    """The origin must reach canonical tags, sitemap.xml and robots.txt."""

    def test_canonical_url_uses_public_origin(self, builder):
        builder.public_origin = "https://docs.example.test"
        builder.build_page("base.html", "docs/page.html", "T", "D", "docs/page.html")

        html = (builder.output_dir / "docs" / "page.html").read_text(encoding="utf-8")
        assert 'href="https://docs.example.test/docs/page.html"' in html
        assert DEFAULT_PUBLIC_ORIGIN not in html

    def test_sitemap_and_robots_use_public_origin(self, builder):
        builder.output_dir.mkdir(parents=True, exist_ok=True)
        builder.public_origin = "https://docs.example.test/"
        builder.generate_seo_files()

        sitemap = (builder.output_dir / "sitemap.xml").read_text(encoding="utf-8")
        robots = (builder.output_dir / "robots.txt").read_text(encoding="utf-8")
        assert "<loc>https://docs.example.test/</loc>" in sitemap
        assert "Sitemap: https://docs.example.test/sitemap.xml" in robots

    def test_dynamic_sitemap_uses_public_origin(self, builder):
        builder.output_dir.mkdir(parents=True, exist_ok=True)
        builder.public_origin = "https://docs.example.test"

        content = builder.generate_dynamic_sitemap(date="2026-01-02", pages=["index.html"])

        assert "<loc>https://docs.example.test/index.html</loc>" in content


class TestBuildCli:
    """``--site-url`` must reach the builder."""

    def test_main_passes_site_url_to_the_builder(
        self, build_main, website_build_module, monkeypatch
    ):
        captured = {}

        class FakeBuilder(website_build_module.WebsiteBuilder):
            def build_site(self, *_args, **_kwargs):
                captured["public_origin"] = self.public_origin
                captured["base_url"] = self.base_url

        monkeypatch.setattr(website_build_module, "WebsiteBuilder", FakeBuilder)
        monkeypatch.setattr(
            "sys.argv",
            ["build.py", "--base-url", "", "--site-url", "https://docs.example.test"],
        )

        assert build_main() == 0
        assert captured["public_origin"] == "https://docs.example.test"
        assert captured["base_url"] == ""
