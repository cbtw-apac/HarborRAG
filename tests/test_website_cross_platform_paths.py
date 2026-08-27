#!/usr/bin/env python3
"""Tests that generated URLs and ordering do not vary with the build platform."""

from pathlib import Path, PurePosixPath, PureWindowsPath

# A Windows Path renders "docs\developers\testing\README.html". Counting "/" in that
# string yields depth 1, so every relative asset link points one level up instead of
# three, and the separators leak into the canonical and Open Graph URLs.
WINDOWS_STYLE_PATH = r"docs\developers\testing\README.html"
POSIX_PATH = "docs/developers/testing/README.html"
PUBLIC_ORIGIN = "https://cbtw-apac.github.io/HarborRAG"


class TestUrlPathNormalization:
    """A site-relative path reaches the templates as a URL, never as an OS path."""

    def test_build_page_computes_relative_depth_from_url_separators(self, builder):
        (builder.templates_dir / "depth.html").write_text(
            '<link rel="stylesheet" href="{{ base_url }}assets/site.css">',
            encoding="utf-8",
        )

        builder.build_page(
            "depth.html", WINDOWS_STYLE_PATH, "Testing", "Testing guide", WINDOWS_STYLE_PATH
        )

        html = (builder.output_dir / POSIX_PATH).read_text(encoding="utf-8")
        assert 'href="../../../assets/site.css"' in html

    def test_build_page_emits_slash_only_canonical_url(self, builder):
        builder.build_page(
            "base.html",
            WINDOWS_STYLE_PATH,
            "Testing",
            "Testing guide",
            WINDOWS_STYLE_PATH,
            "<p>x</p>",
        )

        html = (builder.output_dir / POSIX_PATH).read_text(encoding="utf-8")
        assert f'<link rel="canonical" href="{PUBLIC_ORIGIN}/{POSIX_PATH}">' in html

    def test_nested_docs_page_urls_use_posix_separators(self, builder):
        nested = Path("docs/developers/testing/guide.md")
        nested.parent.mkdir(parents=True, exist_ok=True)
        nested.write_text("# Guide\n\nBody.\n", encoding="utf-8")

        structure = builder.build_docs_structure()

        urls = [child["url"] for child in structure["children"]]
        assert "docs/developers/testing/guide.html" in urls
        assert all("\\" not in url for url in urls)


class TestSitemapDeterminism:
    """The sitemap must not inherit filesystem or platform-specific ordering."""

    def test_dynamic_sitemap_orders_pages_case_sensitively(self, builder):
        # WindowsPath compares case-insensitively, which would order "developers"
        # before "LICENSE"; sorting the URL strings orders them the same everywhere.
        for relative in ("index.html", "docs/developers/README.html", "docs/LICENSE.html"):
            path = builder.output_dir / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("<html></html>", encoding="utf-8")

        sitemap = builder.generate_dynamic_sitemap(date="2026-01-01")

        locs = [
            line.split("<loc>")[1].split("</loc>")[0]
            for line in sitemap.splitlines()
            if "<loc>" in line
        ]
        assert locs == sorted(locs)
        assert locs.index(f"{PUBLIC_ORIGIN}/docs/LICENSE.html") < locs.index(
            f"{PUBLIC_ORIGIN}/docs/developers/README.html"
        )


class TestPathRenderingAssumption:
    """Documents the platform behaviour the fixes above guard against."""

    def test_windows_and_posix_paths_render_differently(self):
        assert str(PureWindowsPath(POSIX_PATH)) == WINDOWS_STYLE_PATH
        assert str(PurePosixPath(POSIX_PATH)) == POSIX_PATH
        assert PureWindowsPath(POSIX_PATH).as_posix() == POSIX_PATH
