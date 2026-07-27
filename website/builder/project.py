"""ProjectStructureMixin implementation for the website builder."""

import json
from pathlib import Path


class ProjectStructureMixin:
    """Focused website-build operations composed by ``WebsiteBuilder``."""

    def generate_project_info(self, **kwargs) -> dict:
        """Generate project information for templates."""
        project_info = {
            "name": "QDrant Loader",
            "version": "0.4.0b1",
            "description": "Enterprise-ready vector database toolkit",
            "github_url": "https://github.com/martin-papy/qdrant-loader",
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
                # Normalize workspace naming to product name
                if isinstance(project_info.get("name"), str) and project_info["name"].endswith(
                    "-workspace"
                ):
                    project_info["name"] = "QDrant Loader"

                # Try to get homepage/repository from pyproject urls
                urls = project_section.get("urls", {}) if isinstance(project_section, dict) else {}
                homepage = urls.get("Homepage")
                if homepage and not getattr(self, "base_url_user_set", False) and not self.base_url:
                    # Set base_url from pyproject if not provided externally
                    self.base_url = homepage.rstrip("/")
                repo_url = urls.get("Repository") or urls.get("Source")
                if repo_url:
                    project_info["github_url"] = repo_url
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
        """Build documentation navigation structure."""
        # Simplified navigation building
        docs_dir = Path("docs")
        if not docs_dir.exists():
            return {}

        nav_data = {"title": "Documentation", "children": []}

        for item in sorted(docs_dir.iterdir()):
            if item.is_file() and item.suffix == ".md":
                nav_data["children"].append(
                    {
                        "title": self._humanize_title(item.stem),
                        "url": f"docs/{item.name}",
                    }
                )
            elif item.is_dir():
                nav_data["children"].append(
                    {
                        "title": self._humanize_title(item.name),
                        "url": f"docs/{item.name}/",
                    }
                )

        self.docs_nav_data = nav_data
        return nav_data

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
