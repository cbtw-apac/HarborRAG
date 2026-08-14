#!/usr/bin/env python3
"""Tests for website/builder/seo.py — sitemap and robots.txt generation."""


class TestSeoBuildMixin:
    """Test sitemap and robots.txt generation."""

    def test_generate_seo_files_uses_default_host(self, builder):
        builder.output_dir.mkdir(parents=True, exist_ok=True)
        builder.generate_seo_files()

        sitemap = (builder.output_dir / "sitemap.xml").read_text(encoding="utf-8")
        robots = (builder.output_dir / "robots.txt").read_text(encoding="utf-8")

        assert "<loc>https://cbtw-apac.github.io/HarborRAG/</loc>" in sitemap
        assert "<loc>https://cbtw-apac.github.io/HarborRAG/docs/</loc>" in sitemap
        assert "Sitemap: https://cbtw-apac.github.io/HarborRAG/sitemap.xml" in robots

    def test_generate_seo_files_uses_configured_base_url(self, builder):
        builder.output_dir.mkdir(parents=True, exist_ok=True)
        builder.base_url = "https://docs.example.test/"
        builder.generate_seo_files()

        sitemap = (builder.output_dir / "sitemap.xml").read_text(encoding="utf-8")
        robots = (builder.output_dir / "robots.txt").read_text(encoding="utf-8")

        assert "<loc>https://docs.example.test/</loc>" in sitemap
        assert "Sitemap: https://docs.example.test/sitemap.xml" in robots
        assert "example.com" not in robots

    def test_generate_robots_file_only(self, builder):
        builder.output_dir.mkdir(parents=True, exist_ok=True)
        builder.base_url = "https://docs.example.test"
        builder.generate_robots_file()

        robots = (builder.output_dir / "robots.txt").read_text(encoding="utf-8")
        assert robots.startswith("User-agent: *")
        assert "Sitemap: https://docs.example.test/sitemap.xml" in robots
        assert not (builder.output_dir / "sitemap.xml").exists()

    def test_generate_dynamic_sitemap_with_explicit_pages_and_date(self, builder):
        builder.output_dir.mkdir(parents=True, exist_ok=True)

        content = builder.generate_dynamic_sitemap(
            date="2026-01-02", pages=["index.html", "docs/index.html"]
        )

        assert "<lastmod>2026-01-02</lastmod>" in content
        assert "<loc>https://cbtw-apac.github.io/HarborRAG/index.html</loc>" in content
        assert "<loc>https://cbtw-apac.github.io/HarborRAG/docs/index.html</loc>" in content
        assert (builder.output_dir / "sitemap.xml").read_text(encoding="utf-8") == content

    def test_generate_dynamic_sitemap_discovers_pages(self, builder):
        (builder.output_dir / "docs").mkdir(parents=True, exist_ok=True)
        (builder.output_dir / "index.html").write_text("<html></html>", encoding="utf-8")
        (builder.output_dir / "docs" / "guide.html").write_text("<html></html>", encoding="utf-8")

        content = builder.generate_dynamic_sitemap()

        assert "index.html" in content
        assert "docs/guide.html" in content

    def test_generate_dynamic_sitemap_with_missing_output_dir(self, builder):
        content = builder.generate_dynamic_sitemap(date="2026-01-02")

        assert content.endswith("</urlset>")
        assert "<url>" not in content
