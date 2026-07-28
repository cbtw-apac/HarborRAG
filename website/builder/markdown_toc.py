"""MarkdownTocMixin implementation."""

import re


class MarkdownTocMixin:
    """Focused Markdown operations composed by ``MarkdownProcessor``."""

    def render_toc(self, html_content: str) -> str:
        """Generate table of contents from HTML headings."""
        # Find all headings (capture inner HTML, allow multiline)
        heading_pattern = r'<(h[1-6])[^>]*id="([^\"]+)"[^>]*>(.*?)</h[1-6]>'
        headings = re.findall(heading_pattern, html_content, flags=re.DOTALL)

        if not headings:
            return ""

        toc_html = '<div class="toc"><h3>Table of Contents</h3>'

        # Build hierarchical structure
        current_level = 0
        open_lists = 0

        import html as _html

        # Default leaf icon (small, neutral color)
        default_svg = (
            '<svg class="toc-icon" style="width:1rem;height:1rem;object-fit:contain;vertical-align:middle;margin-right:0.35rem;" '
            'width="20" height="20" viewBox="0 0 20 20" fill="none" xmlns="http://www.w3.org/2000/svg">'
            '<path d="M10 7.5C9.50555 7.5 9.0222 7.64662 8.61108 7.92133C8.19995 8.19603 7.87952 8.58648 7.6903 9.04329C7.50108 9.50011 7.45157 10.0028 7.54804 10.4877C7.6445 10.9727 7.8826 11.4181 8.23223 11.7678C8.58187 12.1174 9.02732 12.3555 9.51228 12.452C9.99723 12.5484 10.4999 12.4989 10.9567 12.3097C11.4135 12.1205 11.804 11.8 12.0787 11.3889C12.3534 10.9778 12.5 10.4945 12.5 10C12.5 9.33696 12.2366 8.70107 11.7678 8.23223C11.2989 7.76339 10.663 7.5 10 7.5ZM10 11.25C9.75277 11.25 9.5111 11.1767 9.30554 11.0393C9.09998 10.902 8.93976 10.7068 8.84515 10.4784C8.75054 10.2499 8.72579 9.99861 8.77402 9.75614C8.82225 9.51366 8.9413 9.29093 9.11612 9.11612C9.29093 8.9413 9.51366 8.82225 9.75614 8.77402C9.99861 8.72579 10.2499 8.75054 10.4784 8.84515C10.7068 8.93976 10.902 9.09998 11.0393 9.30554C11.1767 9.5111 11.25 9.75277 11.25 10C11.25 10.3315 11.1183 10.6495 10.8839 10.8839C10.6495 11.1183 10.3315 11.25 10 11.25Z" fill="#343330" />'
            "</svg>"
        )

        for idx, (tag, heading_id, text) in enumerate(headings):
            level = int(tag[1])  # Extract number from h1, h2, etc.

            # Determine if this heading has child headings (deeper level) before next sibling
            has_child = False
            for next_tag, _next_id, _next_text in headings[idx + 1 :]:
                next_level = int(next_tag[1])
                if next_level > level:
                    has_child = True
                    break
                if next_level <= level:
                    break

            # Handle level changes
            if level > current_level:
                # Open new nested lists for deeper levels
                while current_level < level:
                    if current_level == 0:
                        toc_html += "<ul>"
                    else:
                        toc_html += "<ul>"
                    open_lists += 1
                    current_level += 1
            elif level < current_level:
                # Close lists for shallower levels
                while current_level > level:
                    toc_html += "</ul>"
                    open_lists -= 1
                    current_level -= 1

            # Extract first <img> if present and sanitize it for TOC display
            icon_html = ""
            img_match = re.search(r"(<img[^>]*>)", text, flags=re.DOTALL)
            if img_match:
                icon_html = img_match.group(1)
                # Remove any on* handlers and javascript: hrefs for safety
                icon_html = re.sub(r"\s(on\w+)\s*=\s*(\"[^\"]*\"|'[^']*')", "", icon_html)
                icon_html = re.sub(r"javascript:\s*", "", icon_html, flags=re.IGNORECASE)
                # Remove any existing size/style attributes so we can normalize appearance
                icon_html = re.sub(r"\s(width|height)=\s*(\"[^\"]*\"|'[^']*')", "", icon_html)
                icon_html = re.sub(r"\sstyle=\s*(\"[^\"]*\"|'[^']*')", "", icon_html)
                # Ensure a small consistent size and spacing for TOC icons
                # Add class toc-icon (append if class exists)
                if re.search(r"\sclass=\s*\"[^\"]+\"", icon_html):
                    icon_html = re.sub(
                        r"\sclass=\s*\"([^\"]+)\"",
                        lambda m: f' class="{m.group(1)} toc-icon"',
                        icon_html,
                    )
                elif re.search(r"\sclass=\s*'[^']+'", icon_html):
                    icon_html = re.sub(
                        r"\sclass=\s*'([^']+)'",
                        lambda m: f" class='{m.group(1)} toc-icon'",
                        icon_html,
                    )
                else:
                    # inject class and inline style before the closing >
                    icon_html = (
                        icon_html.rstrip(">")
                        + ' class="toc-icon" style="width:1rem;height:1rem;object-fit:contain;vertical-align:middle;margin-right:0.35rem;">'
                    )

            # Derive display text: strip HTML, or use img alt, or fallback to id
            display_text = re.sub(r"<[^>]+>", "", text).strip()
            if not display_text:
                m = re.search(r'<img[^>]*alt=["\']([^"\']+)["\']', text)
                if m:
                    display_text = m.group(1).strip()
                else:
                    display_text = heading_id

            display_text = _html.escape(_html.unescape(display_text))

            # If no icon and this is a level-3 leaf heading (and not already a numbered item), use the default SVG
            starts_with_number = bool(re.match(r"^\d+\.", display_text))
            if not icon_html and not has_child and level == 3 and not starts_with_number:
                icon_html = default_svg

            toc_html += f'<li class="list-group-item"><a href="#{heading_id}">{icon_html}{display_text}</a></li>\n'
        # Close all remaining open lists
        while open_lists > 0:
            toc_html += "</ul>"
            open_lists -= 1

        toc_html += "</div>"

        return toc_html
