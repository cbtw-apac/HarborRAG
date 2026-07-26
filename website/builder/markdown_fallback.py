"""MarkdownFallbackMixin implementation."""

import re


class MarkdownFallbackMixin:
    """Focused Markdown operations composed by ``MarkdownProcessor``."""

    def _basic_markdown_to_html_no_regex(self, markdown_content: str) -> str:
        """Basic markdown to HTML conversion without regex."""
        content = markdown_content
        if not content.strip():
            return ""

        def transform_inline(text: str) -> str:
            # Bold (strong) and italics (em)
            text = re.sub(r"\*\*([^*]+)\*\*", lambda m: f"<strong>{m.group(1)}</strong>", text)
            text = re.sub(r"\*([^*]+)\*", lambda m: f"<em>{m.group(1)}</em>", text)
            # Inline code
            text = re.sub(r"`([^`]+)`", lambda m: f"<code>{m.group(1)}</code>", text)
            # Links [text](url)
            text = re.sub(
                r"\[([^\]]+)\]\(([^)]+)\)",
                lambda m: f'<a href="{m.group(2)}">{m.group(1)}</a>',
                text,
            )
            return text

        lines = content.split("\n")
        html_lines: list[str] = []
        in_code_block = False
        in_list = False

        for line in lines:
            raw = line.rstrip("\n")
            stripped = raw.lstrip()
            if stripped.startswith("```"):
                if in_code_block:
                    html_lines.append("</code></pre>")
                    in_code_block = False
                else:
                    # close any open list before starting code block
                    if in_list:
                        html_lines.append("</ul>")
                        in_list = False
                    html_lines.append("<pre><code>")
                    in_code_block = True
                continue

            if in_code_block:
                html_lines.append(raw)
                continue

            # Headings
            if raw.startswith("# "):
                if in_list:
                    html_lines.append("</ul>")
                    in_list = False
                html_lines.append(f"<h1>{transform_inline(raw[2:])}</h1>")
                continue
            if raw.startswith("## "):
                if in_list:
                    html_lines.append("</ul>")
                    in_list = False
                html_lines.append(f"<h2>{transform_inline(raw[3:])}</h2>")
                continue
            if raw.startswith("### "):
                if in_list:
                    html_lines.append("</ul>")
                    in_list = False
                html_lines.append(f"<h3>{transform_inline(raw[4:])}</h3>")
                continue
            if raw.startswith("#### "):
                if in_list:
                    html_lines.append("</ul>")
                    in_list = False
                html_lines.append(f"<h4>{transform_inline(raw[5:])}</h4>")
                continue
            if raw.startswith("##### "):
                if in_list:
                    html_lines.append("</ul>")
                    in_list = False
                html_lines.append(f"<h5>{transform_inline(raw[6:])}</h5>")
                continue
            if raw.startswith("###### "):
                if in_list:
                    html_lines.append("</ul>")
                    in_list = False
                html_lines.append(f"<h6>{transform_inline(raw[7:])}</h6>")
                continue

            # Lists
            if raw.lstrip().startswith("- "):
                if not in_list:
                    html_lines.append("<ul>")
                    in_list = True
                item_text = raw.lstrip()[2:]
                html_lines.append(f"<li>{transform_inline(item_text)}</li>")
                continue
            else:
                if in_list and raw.strip() == "":
                    html_lines.append("</ul>")
                    in_list = False

            # Paragraphs
            if raw.strip():
                html_lines.append(f"<p>{transform_inline(raw)}</p>")

        # Close any open list
        if in_list:
            html_lines.append("</ul>")

        # Join and strip extraneous blank lines
        html = "\n".join([h for h in html_lines if h is not None])
        # Apply Bootstrap classes and heading IDs
        return html
