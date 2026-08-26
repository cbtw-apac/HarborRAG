"""Fixture plugin for mock_project_structure."""

import pytest


@pytest.fixture
def mock_project_structure(temp_workspace):
    """Create a mock project structure for testing."""
    # Create directory structure
    (temp_workspace / "website" / "templates").mkdir(parents=True)
    (temp_workspace / "website" / "assets" / "logos").mkdir(parents=True)
    (temp_workspace / "docs").mkdir()
    (temp_workspace / "coverage-artifacts").mkdir()
    (temp_workspace / "test-results").mkdir()

    # Create pyproject.toml
    pyproject_content = """[project]
name = "qdrant-loader"
version = "0.4.0"
description = "Vector database toolkit for building searchable knowledge bases"
authors = [{name = "Martin Papy", email = "martin.papy@example.com"}]

[project.optional-dependencies]
docs = [
    "tomli>=2.0.0",
    "markdown>=3.5.0",
    "pygments>=2.15.0",
    "cairosvg>=2.7.0",
    "pillow>=10.0.0"
]
"""
    (temp_workspace / "pyproject.toml").write_text(pyproject_content)

    # Create basic templates
    base_template = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{{ page_title }} - QDrant Loader</title>
    <meta name="description" content="{{ page_description }}">
    <link rel="canonical" href="{{ canonical_url }}">
    <meta name="author" content="{{ author }}">
    <meta name="version" content="{{ version }}">
</head>
<body>
    <main>{{ content }}</main>
</body>
</html>"""

    index_template = """<div class="hero">
    <h1>Welcome to QDrant Loader</h1>
    <p>Enterprise-ready vector database toolkit</p>
</div>"""

    docs_template = """<div class="docs">
    <h1>Documentation</h1>
    <p>Comprehensive documentation for QDrant Loader</p>
</div>"""

    privacy_policy_template = """<section class="py-5">
    <div class="container">
        <div class="row justify-content-center">
            <div class="col-lg-10">
                <h1 class="display-5 fw-bold text-primary mb-4">
                    <i class="bi bi-shield-check me-3"></i>Privacy Policy
                </h1>
                <p class="lead text-muted mb-5">
                    Your privacy is important to us. This privacy policy explains how we collect, use, and protect your information.
                </p>

                <div class="card border-0 shadow">
                    <div class="card-body p-5">
                        <h2 class="h4 fw-bold mb-3">Information We Collect</h2>
                        <p class="mb-4">
                            We may collect information you provide directly to us, such as when you contact us or use our services.
                        </p>

                        <h2 class="h4 fw-bold mb-3">How We Use Your Information</h2>
                        <p class="mb-4">
                            We use the information we collect to provide, maintain, and improve our services.
                        </p>

                        <h2 class="h4 fw-bold mb-3">Information Sharing</h2>
                        <p class="mb-4">
                            We do not sell, trade, or otherwise transfer your personal information to third parties.
                        </p>

                        <h2 class="h4 fw-bold mb-3">Contact Us</h2>
                        <p class="mb-0">
                            If you have any questions about this privacy policy, please contact us.
                        </p>

                        <div class="mt-4 text-muted small">
                            <p>Last updated: {{ last_updated }}</p>
                        </div>
                    </div>
                </div>
            </div>
        </div>
    </div>
</section>"""

    robots_template = """User-agent: *
Allow: /
Sitemap: https://qdrant-loader.net/sitemap.xml"""

    # Write templates
    templates_dir = temp_workspace / "website" / "templates"
    (templates_dir / "base.html").write_text(base_template)
    (templates_dir / "index.html").write_text(index_template)
    (templates_dir / "docs-index.html").write_text(docs_template)
    (templates_dir / "privacy-policy.html").write_text(privacy_policy_template)
    (templates_dir / "robots.txt").write_text(robots_template)

    # Create sample documentation files
    (temp_workspace / "README.md").write_text("""# QDrant Loader

Enterprise-ready vector database toolkit for building searchable knowledge bases.

## Features

- Multi-source data loading
- Vector embeddings
- Search capabilities
""")

    (temp_workspace / "docs" / "installation.md").write_text("""# Installation

Install QDrant Loader using pip:

```bash
pip install qdrant-loader
```
""")

    # Create sample assets
    assets_dir = temp_workspace / "website" / "assets"
    (assets_dir / "style.css").write_text("body { font-family: Arial, sans-serif; }")
    (assets_dir / "script.js").write_text("console.log('QDrant Loader loaded');")

    # Create sample SVG logo
    svg_content = """<?xml version="1.0" encoding="UTF-8"?>
<svg width="100" height="100" xmlns="http://www.w3.org/2000/svg">
    <rect width="100" height="100" fill="#667eea"/>
    <text x="50" y="55" text-anchor="middle" fill="white" font-size="20">Q</text>
</svg>"""
    (assets_dir / "logos" / "qdrant-loader-icon.svg").write_text(svg_content)

    return temp_workspace
