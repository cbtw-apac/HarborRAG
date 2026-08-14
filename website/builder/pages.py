"""PageBuildMixin implementation for the website builder."""

from pathlib import Path


class PageBuildMixin:
    """Focused website-build operations composed by ``WebsiteBuilder``."""

    def build_page(
        self,
        template_name: str,
        output_filename: str,
        title: str,
        description: str,
        canonical_path: str,
        content: str = "",
        **extra_replacements,
    ) -> None:
        """Build a single page from template."""
        template_content = self.load_template(template_name)

        # Load a content template if available when no explicit content is given.
        # For pages where output and canonical differ, missing content should raise.
        # For pages where they are the same (e.g., index.html), load content if
        # the template exists, otherwise fall back to empty content.
        if not content:
            try:
                content = self.load_template(output_filename)
            except FileNotFoundError:
                if output_filename != canonical_path:
                    # Maintain behavior for explicit content templates
                    raise
                # Otherwise, leave content empty

        project_info = self.generate_project_info()

        # Calculate base URL for relative paths
        if canonical_path.count("/") > 0:
            base_url = "../" * canonical_path.count("/")
        else:
            # Normalize root base URL
            if self.base_url:
                base_url = self.base_url.rstrip("/") + "/"
            else:
                base_url = "./"

        # Merge extra replacements ensuring defaults for optional placeholders
        extras = dict(extra_replacements)
        extras.setdefault("additional_head", "")
        extras.setdefault("additional_scripts", "")

        replacements = {
            "page_title": title,
            "page_description": description,
            "content": content,
            "base_url": base_url,
            "canonical_url": (
                self.base_url.rstrip("/")
                if self.base_url
                else "https://cbtw-apac.github.io/HarborRAG"
            )
            + "/"
            + canonical_path,
            "author": project_info.get("name", "HarborRAG"),
            "version": project_info.get("version", "2.0.0"),
            "project_name": project_info["name"],
            "project_version": project_info["version"],
            "project_status": project_info.get("status", "Alpha"),
            "project_license": project_info.get("license", "Apache-2.0"),
            "project_commit": project_info.get("commit", {}).get("short", ""),
            "project_description": project_info["description"],
            "github_url": project_info["github_url"],
            "issues_url": project_info["issues_url"],
            "documentation_url": project_info["documentation_url"],
            **extras,
        }

        final_content = self.replace_placeholders(template_content, replacements)

        output_path = self.output_dir / output_filename
        output_path.parent.mkdir(parents=True, exist_ok=True)

        with open(output_path, "w", encoding="utf-8") as f:
            f.write(final_content)

        print(f"📄 Built {output_filename}")

    def build_markdown_page(
        self,
        markdown_file: str,
        output_path: str,
        title: str = "",
        breadcrumb: str = "",
        **kwargs,
    ) -> None:
        """Build a page from markdown file."""
        markdown_path = Path(markdown_file)
        if not markdown_path.exists():
            print(f"⚠️  Markdown file not found: {markdown_file}, skipping page generation")
            return

        try:
            with open(markdown_path, encoding="utf-8") as f:
                markdown_content = f.read()
        except Exception as e:
            print(f"⚠️  Failed to read markdown file {markdown_file}: {e}")
            return

        # Extract title if not provided
        if not title:
            title = self.extract_title_from_markdown(markdown_content)

        # Normalize links in markdown before conversion
        markdown_content = self.markdown_processor.convert_markdown_links_to_html(
            markdown_content, str(markdown_path)
        )

        # Convert markdown to HTML
        html_content = self.markdown_to_html(markdown_content, str(markdown_path), output_path)
        # Normalize any remaining HTML hrefs
        html_content = self.markdown_processor.convert_markdown_links_to_html(
            html_content, str(markdown_path), output_path
        )

        # Build a Table of Contents and wrap in docs layout
        toc_html = self.render_toc(html_content)
        if toc_html:
            toc_html = self.add_bootstrap_classes(toc_html)

        wrapped_content = f"""
<section>
    <div class=\"container-fluid\">
    <div class=\"row toc-layout\">
      <aside class=\"toc-sidebar d-none d-lg-block p-0\">
        <div class=\"position-sticky\">
          {toc_html or '<div class="text-muted small">No sections</div>'}
        </div>
      </aside>
      <div class=\"container-content\">
        {html_content}
      </div>
    </div>
</div>
</section>
"""

        # Build the page
        self.build_page(
            "base.html",
            output_path,
            title,
            f"{title} - HarborRAG",
            output_path,
            content=wrapped_content,
            breadcrumb=breadcrumb,
            **kwargs,
        )

    def build_license_page(
        self,
        source_file: str = "LICENSE",
        output_file: str = "license.html",
        title: str = "License",
        description: str = "License",
    ) -> None:
        """Build license page from LICENSE file."""
        license_path = Path(source_file)
        if not license_path.exists():
            print(f"⚠️  License file not found: {source_file}, skipping license page")
            return

        try:
            with open(license_path, encoding="utf-8") as f:
                license_content = f.read()

            # Create license page with heading
            html_content = f"""
            <h1>License Information</h1>
            <div class="license-content">
                <pre>{license_content}</pre>
            </div>
            """

            self.build_page(
                "base.html",
                output_file,
                title,
                description,
                output_file,
                content=html_content,
            )
        except Exception as e:
            print(f"⚠️  Failed to build license page: {e}")
