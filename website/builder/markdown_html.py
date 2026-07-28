"""MarkdownHtmlMixin implementation."""

import re


class MarkdownHtmlMixin:
    """Focused Markdown operations composed by ``MarkdownProcessor``."""

    def fix_malformed_code_blocks(self, html_content: str) -> str:
        """Fix code blocks that weren't properly converted by markdown."""

        # Fix single-line code snippets that should be code blocks
        # Convert paragraphs with inline code containing bash commands to proper code blocks
        html_content = re.sub(
            r'<p><code class="inline-code">(bash|sh)\s*\n\s*([^<]+)</code></p>',
            r'<div class="code-block-wrapper"><pre class="code-block"><code class="language-\1">\2</code></pre></div>',
            html_content,
        )

        # Fix paragraphs with bash/shell commands (with or without language prefix)
        html_content = re.sub(
            r'<p><code class="inline-code">(?:bash\s*\n\s*)?([^<]*(?:mkdir|cd|pip|uv|qdrant-loader|mcp-)[^<]*)</code></p>',
            r'<div class="code-block-wrapper"><pre class="code-block"><code class="language-bash">\1</code></pre></div>',
            html_content,
        )

        # Also handle cases where there's no class attribute
        html_content = re.sub(
            r"<p><code>(?:bash\s*\n\s*)?([^<]*(?:mkdir|cd|pip|uv|qdrant-loader|mcp-)[^<]*)</code></p>",
            r'<div class="code-block-wrapper"><pre class="code-block"><code class="language-bash">\1</code></pre></div>',
            html_content,
        )

        # Clean up stray <p> tags inside code blocks
        html_content = re.sub(
            r"(<code[^>]*>.*?)</p>\s*<p>(.*?</code>)",
            r"\1\n\2",
            html_content,
            flags=re.DOTALL,
        )

        # Fix paragraphs that contain triple backticks (malformed code blocks)
        def fix_code_block(match):
            content = match.group(1)
            # Extract language if present
            lines = content.split("\n")
            first_line = lines[0].strip()
            if first_line.startswith("```"):
                language = first_line[3:].strip()
                code_content = "\n".join(lines[1:])
                # Remove trailing ``` if present
                if code_content.endswith("```"):
                    code_content = code_content[:-3].rstrip()
                return f'<div class="code-block-wrapper"><pre class="code-block"><code class="language-{language}">{code_content}</code></pre></div>'
            return match.group(0)

        # Match paragraphs containing code blocks
        html_content = re.sub(
            r"<p>(```[^`]*```)</p>", fix_code_block, html_content, flags=re.DOTALL
        )

        # Handle multi-paragraph code blocks
        html_content = re.sub(
            r"<p>```(\w+)\s*</p>\s*<p>(.*?)</p>\s*<p>```</p>",
            r'<div class="code-block-wrapper"><pre class="code-block"><code class="language-\1">\2</code></pre></div>',
            html_content,
            flags=re.DOTALL,
        )

        # Handle code blocks split across multiple paragraphs
        html_content = re.sub(
            r"<p>```(\w+)?\s*(.*?)\s*```</p>",
            lambda m: (
                f'<div class="code-block-wrapper"><pre class="code-block"><code class="language-{m.group(1) or ""}">{m.group(2)}</code></pre></div>'
            ),
            html_content,
            flags=re.DOTALL,
        )

        return html_content

    def ensure_heading_ids(self, html_content: str) -> str:
        """Ensure all headings have IDs for anchor links."""

        def slugify(text: str) -> str:
            """Convert text to URL-safe slug."""
            import re

            slug = re.sub(r"[^\w\s-]", "", text.lower())
            return re.sub(r"[-\s]+", "-", slug).strip("-")

        def _extract_text(html: str) -> str:
            """Return visible text for a piece of HTML (fall back to img alt)."""
            # Remove tags to get visible text
            text_only = re.sub(r"<[^>]+>", "", html).strip()
            if text_only:
                return text_only
            # If no visible text, try to get alt from first <img>
            m = re.search(r'<img[^>]*alt=["\']([^"\']+)["\']', html)
            if m:
                return m.group(1).strip()
            return ""

        def add_id(match: re.Match) -> str:
            """Add ID to heading if not present."""
            tag = match.group(1)
            attrs = match.group(2) or ""
            content = match.group(3) or ""

            if "id=" not in attrs:
                visible = _extract_text(content)
                heading_id = slugify(visible or content)
                if attrs:
                    attrs = f' id="{heading_id}" {attrs.strip()}'
                else:
                    attrs = f' id="{heading_id}"'

            return f"<{tag}{attrs}>{content}</{tag}>"

        # Match headings even when they contain HTML inside
        heading_pattern = r"<(h[1-6])([^>]*)>(.*?)</h[1-6]>"
        return re.sub(heading_pattern, add_id, html_content, flags=re.DOTALL)

    def add_bootstrap_classes(self, html_content: str) -> str:
        """Add Bootstrap classes to HTML elements."""

        def add_classes_to_tag(attrs: str, classes_to_add: str) -> str:
            class_match = re.search(r'class="([^"]*)"', attrs)
            if class_match:
                existing = class_match.group(1).split()
                for cls in classes_to_add.split():
                    if cls not in existing:
                        existing.append(cls)
                return re.sub(
                    r'class="([^"]*)"',
                    f'class="{" ".join(existing)}"',
                    attrs,
                    count=1,
                )
            return f'{attrs} class="{classes_to_add}"'

        # Add Bootstrap header classes
        html_content = re.sub(
            r"<h1([^>]*)>",
            r'<h1\1 class="display-4 fw-bold text-primary mb-4">',
            html_content,
        )
        html_content = re.sub(
            r"<h2([^>]*)>",
            r'<h2\1 class="h2 fw-bold text-primary">',
            html_content,
        )
        html_content = re.sub(
            r"<h3([^>]*)>",
            r'<h3\1 class="h3 fw-bold text-primary">',
            html_content,
        )
        html_content = re.sub(r"<h4([^>]*)>", r'<h4\1 class="h4 fw-bold">', html_content)
        html_content = re.sub(r"<h5([^>]*)>", r'<h5\1 class="h5 fw-bold">', html_content)
        html_content = re.sub(r"<h6([^>]*)>", r'<h6\1 class="h6 fw-semibold">', html_content)

        # Add Bootstrap code block classes - clean approach
        # First handle codehilite divs
        html_content = re.sub(
            r'<div class="codehilite">',
            '<div class="code-block-wrapper">',
            html_content,
        )

        # Handle standalone pre blocks (not already in wrappers)
        html_content = re.sub(
            r'(?<!<div class="code-block-wrapper">)<pre>',
            '<div class="code-block-wrapper"><pre class="code-block">',
            html_content,
        )

        # Add code-block class to pre tags that don't have it
        html_content = re.sub(
            r'<pre(?![^>]*class="code-block")([^>]*)>',
            r'<pre class="code-block"\1>',
            html_content,
        )

        # Close wrapper divs only for pre blocks that we wrapped
        html_content = re.sub(
            r'(<div class="code-block-wrapper"><pre class="code-block"[^>]*>.*?)</pre>(?!</div>)',
            r"\1</pre></div>",
            html_content,
            flags=re.DOTALL,
        )

        # Normalize codehilite/Pygments token spans so code text stays contiguous.
        # This keeps HTML stable for tests and lets our client-side highlighter style code.
        html_content = re.sub(r"<span[^>]*>", "", html_content)
        html_content = re.sub(r"</span>", "", html_content)
        # Add Bootstrap inline code classes
        # First handle code blocks, then inline code
        html_content = re.sub(
            r"<code>",
            '<code class="inline-code">',
            html_content,
        )
        # Override inline-code class for code inside pre blocks
        html_content = re.sub(
            r'(<pre[^>]*>.*?)<code class="inline-code">',
            r"\1<code>",
            html_content,
            flags=re.DOTALL,
        )

        # Add Bootstrap link classes
        html_content = re.sub(
            r'<a([^>]*?)href="([^"]*)"([^>]*?)>',
            r'<a\1href="\2"\3 class="text-decoration-none">',
            html_content,
        )

        # Normalize numbered step paragraphs into ordered-list items.
        # Some markdown flows with fenced code blocks are rendered as:
        # <p>1. <strong>Step</strong></p>
        # ...code block...
        # <p>2. <strong>Step</strong></p>
        # Converting these to <ol start="N"><li>...</li></ol> preserves the
        # existing list-card CSS while keeping numbering stable.
        html_content = re.sub(
            r"<p>\s*(\d+)\.\s*(<strong>.*?</strong>.*?)</p>",
            r'<ol start="\1"><li>\2</li></ol>',
            html_content,
            flags=re.DOTALL,
        )

        # Add Bootstrap list classes
        html_content = re.sub(
            r"<ul([^>]*)>",
            lambda m: f"<ul{add_classes_to_tag(m.group(1), 'list-group list-group-flush')}>",
            html_content,
        )
        html_content = re.sub(
            r"<ol([^>]*)>",
            lambda m: f"<ol{add_classes_to_tag(m.group(1), 'list-group list-group-numbered')}>",
            html_content,
        )
        html_content = re.sub(
            r"<li([^>]*)>",
            lambda m: f"<li{add_classes_to_tag(m.group(1), 'list-group-item')}>",
            html_content,
        )

        # Add Bootstrap table classes
        html_content = re.sub(
            r"<table>", '<table class="table table-striped table-hover">', html_content
        )

        # Add Bootstrap alert classes for blockquotes
        html_content = re.sub(
            r"<blockquote>", '<blockquote class="alert alert-info">', html_content
        )

        # Add Bootstrap button classes to links that look like buttons
        html_content = re.sub(
            r'<a([^>]*?)class="[^"]*btn[^"]*"([^>]*?)>',
            r'<a\1class="btn btn-primary"\2>',
            html_content,
        )

        return html_content

    def render_task_list_checkboxes(self, html_content: str) -> str:
        """Render markdown task-list markers as checkbox inputs."""

        def add_class(attrs: str, class_name: str) -> str:
            class_match = re.search(r'class="([^"]*)"', attrs)
            if class_match:
                classes = class_match.group(1).split()
                if class_name not in classes:
                    classes.append(class_name)
                return re.sub(r'class="([^"]*)"', f'class="{" ".join(classes)}"', attrs)
            return f'{attrs} class="{class_name}"'

        def replace_task_item(match: re.Match) -> str:
            attrs = match.group("attrs") or ""
            marker = match.group("marker")
            body = match.group("body")
            checked_attr = " checked" if marker.lower() == "x" else ""
            attrs = add_class(attrs, "task-list-item")
            return (
                f"<li{attrs}>"
                f'<input class="form-check-input me-2" type="checkbox"{checked_attr} disabled>'
                f"{body}</li>"
            )

        return re.sub(
            r"<li(?P<attrs>[^>]*)>\s*\[(?P<marker>[ xX])\]\s*(?P<body>.*?)</li>",
            replace_task_item,
            html_content,
            flags=re.DOTALL,
        )
