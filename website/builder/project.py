"""Project metadata and canonical documentation navigation."""

import html
import json
import re
from pathlib import Path


class ProjectStructureMixin:
    """Focused website-build operations composed by ``WebsiteBuilder``."""

    def generate_project_info(self, **kwargs) -> dict:
        """Generate project information for templates."""
        project_info = {
            "name": "HarborRAG",
            "version": "2.0.0a1",
            "status": "Alpha",
            "license": "Apache-2.0",
            "description": "A modular, provider-agnostic RAG framework for engineering knowledge",
            "github_url": "https://github.com/cbtw-apac/HarborRAG",
            "issues_url": "https://github.com/cbtw-apac/HarborRAG/issues",
            "documentation_url": "https://github.com/cbtw-apac/HarborRAG/tree/main/docs",
        }

        # Override with any provided kwargs
        project_info.update(kwargs)

        # Try to load from pyproject.toml
        try:
            import tomllib

            with open("pyproject.toml", "rb") as f:
                pyproject = tomllib.load(f)
                project_section = pyproject.get("project", {})
                project_info.update(
                    {
                        "name": project_section.get("name", project_info["name"]),
                        "version": project_section.get("version", project_info["version"]),
                        "description": project_section.get(
                            "description", project_info["description"]
                        ),
                    }
                )
                classifiers = project_section.get("classifiers", [])
                for classifier in classifiers if isinstance(classifiers, list) else []:
                    if isinstance(classifier, str) and classifier.startswith(
                        "Development Status ::"
                    ):
                        project_info["status"] = classifier.rsplit(" - ", 1)[-1]
                        break

                license_value = project_section.get("license")
                if isinstance(license_value, str):
                    project_info["license"] = license_value
                # The root project is a workspace distribution, not the public product name.
                if isinstance(project_info.get("name"), str) and project_info["name"].endswith(
                    "-workspace"
                ):
                    project_info["name"] = "HarborRAG"

                # Try to get homepage/repository from pyproject urls
                urls = project_section.get("urls", {}) if isinstance(project_section, dict) else {}
                repo_url = urls.get("Repository") or urls.get("Source")
                if repo_url:
                    project_info["github_url"] = repo_url
                project_info["issues_url"] = urls.get(
                    "Issues", f"{project_info['github_url'].rstrip('/')}/issues"
                )
                project_info["documentation_url"] = urls.get(
                    "Documentation", f"{project_info['github_url'].rstrip('/')}/tree/main/docs"
                )
        except Exception:
            # Ignore malformed project section entries
            pass

        # Try to get git information
        try:
            import subprocess

            # Get git commit hash
            result = subprocess.run(
                ["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True
            )
            project_info["commit_hash"] = result.stdout.strip()

            # Get git commit date
            result = subprocess.run(
                ["git", "log", "-1", "--format=%ci"],
                capture_output=True,
                text=True,
                check=True,
            )
            project_info["commit_date"] = result.stdout.strip()

        except (subprocess.CalledProcessError, FileNotFoundError):
            # Git not available or not a git repository
            pass

        # Add build metadata
        from datetime import datetime

        commit_hash = project_info.get("commit_hash", "")
        project_info["commit"] = {
            "hash": commit_hash,
            "short": commit_hash[:7] if isinstance(commit_hash, str) else "",
            "date": project_info.get("commit_date", ""),
        }
        project_info["build"] = {"timestamp": datetime.utcnow().isoformat(timespec="seconds") + "Z"}

        # Write project info JSON file
        project_info_path = self.output_dir / "project-info.json"
        project_info_path.parent.mkdir(parents=True, exist_ok=True)
        with open(project_info_path, "w", encoding="utf-8") as f:
            json.dump(project_info, f, indent=2)

        return project_info

    def build_docs_nav(self) -> dict:
        """Parse ``docs/TOC.md`` as the canonical ordered navigation."""
        toc_path = Path("docs/TOC.md")
        if not toc_path.exists():
            return {}

        nav_data: dict = {"title": "Documentation", "children": []}
        current_section: dict | None = None
        heading_pattern = re.compile(r"^##\s+(.+?)\s*$")
        link_pattern = re.compile(r"^\s*-\s+\[([^]]+)]\(([^)]+)\)(?:\s+[—-]\s+(.+))?\s*$")

        for line in toc_path.read_text(encoding="utf-8").splitlines():
            heading = heading_pattern.match(line)
            if heading:
                current_section = {"title": heading.group(1), "children": []}
                nav_data["children"].append(current_section)
                continue

            link = link_pattern.match(line)
            if not link or current_section is None:
                continue
            label, target, description = link.groups()
            current_section["children"].append(
                {
                    "title": label,
                    "url": self._docs_target_to_url(target),
                    "description": description or "",
                }
            )

        self.docs_nav_data = nav_data
        return nav_data

    def _docs_target_to_url(self, target: str) -> str:
        """Translate a TOC Markdown target into a URL relative to ``/docs/``."""
        if re.match(r"^[a-z][a-z0-9+.-]*:", target, re.IGNORECASE):
            return target

        path, separator, fragment = target.partition("#")
        if path.startswith("../packages/") and path.endswith("/README.md"):
            package_name = Path(path).parent.name
            path = f"packages/{package_name}/README.html"
        elif path.startswith("../"):
            root_name = Path(path).name
            if root_name in {"LICENSE", "README", "CHANGELOG", "CONTRIBUTING", "SECURITY"}:
                path = f"{root_name}.html"
            else:
                path = f"{Path(root_name).stem}.html" if root_name.endswith(".md") else root_name
        elif path.endswith(".md"):
            path = f"{path[:-3]}.html"

        return f"{path}{separator}{fragment}" if separator else path

    def render_docs_navigation(self, *, compact: bool = False) -> str:
        """Render canonical navigation as cards for docs and landing pages."""
        nav = self.docs_nav_data or self.build_docs_nav()
        if not nav:
            return '<p class="text-muted">Documentation navigation is unavailable.</p>'

        sections: list[str] = []
        max_sections = 3 if compact else None
        for section in nav["children"][:max_sections]:
            links: list[str] = []
            max_links = 4 if compact else None
            for item in section["children"][:max_links]:
                label = html.escape(item["title"])
                raw_url = item["url"]
                if compact and not re.match(r"^[a-z][a-z0-9+.-]*:", raw_url, re.IGNORECASE):
                    raw_url = f"docs/{raw_url}"
                url = html.escape(raw_url, quote=True)
                description = html.escape(item.get("description", ""))
                detail = f"<small>{description}</small>" if description else ""
                links.append(f'<li><a href="{url}"><span>{label}</span>{detail}</a></li>')
            if links:
                sections.append(
                    '<section class="docs-nav-card">'
                    f"<h2>{html.escape(section['title'])}</h2>"
                    f"<ul>{''.join(links)}</ul>"
                    "</section>"
                )
        return '<div class="docs-nav-grid">' + "".join(sections) + "</div>"

    def build_docs_structure(self) -> dict:
        """Build documentation directory structure."""
        docs_dir = Path("docs")
        structure = {"title": "Documentation", "children": []}

        # Create docs output directory
        docs_output_dir = self.output_dir / "docs"
        docs_output_dir.mkdir(parents=True, exist_ok=True)

        if not docs_dir.exists():
            return structure

        # Process all markdown files in docs
        for item in sorted(docs_dir.rglob("*.md")):
            relative_path = str(item.relative_to(docs_dir))
            output_path = relative_path.replace(".md", ".html")

            structure["children"].append(
                {
                    "title": self._humanize_title(item.stem),
                    "path": relative_path,
                    "url": f"docs/{output_path}",
                }
            )

            # Build the page from markdown
            try:
                self.build_markdown_page(
                    str(item),
                    f"docs/{output_path}",
                    title=self._humanize_title(item.stem),
                )
            except Exception as e:
                print(f"⚠️  Failed to build docs page {item}: {e}")

        return structure
