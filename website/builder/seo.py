"""SeoBuildMixin implementation for the website builder."""

from .constants import resolve_public_origin


class SeoBuildMixin:
    """Focused website-build operations composed by ``WebsiteBuilder``."""

    def generate_seo_files(self) -> None:
        """Generate SEO files like sitemap.xml and robots.txt."""
        from datetime import datetime

        # Determine base site URL
        site_base = resolve_public_origin(self.base_url, getattr(self, "public_origin", None))

        # Get current date for lastmod
        current_date = datetime.now().strftime("%Y-%m-%d")

        # Generate simple sitemap.xml
        sitemap_content = f"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url>
    <loc>{site_base}/</loc>
    <lastmod>{current_date}</lastmod>
    <changefreq>weekly</changefreq>
    <priority>1.0</priority>
  </url>
  <url>
    <loc>{site_base}/docs/</loc>
    <lastmod>{current_date}</lastmod>
    <changefreq>weekly</changefreq>
    <priority>0.8</priority>
  </url>
</urlset>"""

        sitemap_path = self.output_dir / "sitemap.xml"
        with open(sitemap_path, "w", encoding="utf-8") as f:
            f.write(sitemap_content)
        print("📄 Generated sitemap.xml")

        # Generate simple robots.txt
        robots_content = f"""User-agent: *
Allow: /

Sitemap: {self.base_url.rstrip("/") if self.base_url else "https://example.com"}/sitemap.xml
"""

        robots_path = self.output_dir / "robots.txt"
        with open(robots_path, "w", encoding="utf-8") as f:
            f.write(robots_content.replace("https://example.com", site_base))
        print("📄 Generated robots.txt")

    def generate_robots_file(self) -> None:
        """Generate only robots.txt referencing the sitemap URL."""
        site_base = resolve_public_origin(self.base_url, getattr(self, "public_origin", None))
        robots_content = f"""User-agent: *
Allow: /

Sitemap: {site_base}/sitemap.xml
"""
        robots_path = self.output_dir / "robots.txt"
        with open(robots_path, "w", encoding="utf-8") as f:
            f.write(robots_content)
        print("📄 Generated robots.txt")

    def generate_dynamic_sitemap(self, date: str = None, pages: list[str] = None) -> str:
        """Generate dynamic sitemap with custom pages."""
        from datetime import datetime

        base_url = resolve_public_origin(self.base_url, getattr(self, "public_origin", None))

        # Auto-discover pages if not provided
        if pages is None:
            pages = []
            # Find HTML files in site directory
            if self.output_dir.exists():
                # Sort the URL paths rather than the Path objects: rglob order is
                # arbitrary, and WindowsPath compares case-insensitively, so either
                # would let the generated sitemap vary by build platform.
                pages = sorted(
                    html_file.relative_to(self.output_dir).as_posix()
                    for html_file in self.output_dir.rglob("*.html")
                )

        # Use provided date or current date
        if date is None:
            date = datetime.now().strftime("%Y-%m-%d")

        sitemap_content = '<?xml version="1.0" encoding="UTF-8"?>\n'
        sitemap_content += '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'

        for page in pages:
            sitemap_content += "  <url>\n"
            sitemap_content += f"    <loc>{base_url}/{page}</loc>\n"
            sitemap_content += f"    <lastmod>{date}</lastmod>\n"
            sitemap_content += "    <changefreq>weekly</changefreq>\n"
            sitemap_content += "    <priority>0.8</priority>\n"
            sitemap_content += "  </url>\n"

        sitemap_content += "</urlset>"

        # Write sitemap to file
        sitemap_path = self.output_dir / "sitemap.xml"
        sitemap_path.parent.mkdir(parents=True, exist_ok=True)
        with open(sitemap_path, "w", encoding="utf-8") as f:
            f.write(sitemap_content)
        print(f"📄 Generated dynamic sitemap.xml with {len(pages)} pages")

        return sitemap_content
