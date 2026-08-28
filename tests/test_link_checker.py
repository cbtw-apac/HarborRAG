#!/usr/bin/env python3
"""
Tests for the link checker script.
"""

import sys
from pathlib import Path
from unittest.mock import patch

import requests
import responses

# Add website directory to Python path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from website.check_links import LinkChecker


class TestLinkChecker:
    """Test cases for the LinkChecker class."""

    def test_init(self):
        """Test LinkChecker initialization."""
        checker = LinkChecker("http://example.com", max_depth=2)
        assert checker.base_url == "http://example.com"
        assert checker.max_depth == 2
        assert checker.visited_urls == set()
        assert checker.checked_links == set()
        assert checker.dead_links == []
        assert len(checker.broken_links) == 0
        assert isinstance(checker.session, requests.Session)

    def test_init_strips_trailing_slash(self):
        """Test that trailing slash is stripped from base URL."""
        checker = LinkChecker("http://example.com/")
        assert checker.base_url == "http://example.com"

    def test_is_internal_url(self):
        """Test internal URL detection."""
        checker = LinkChecker("http://example.com")

        # Internal URLs
        assert checker.is_internal_url("http://example.com/page")
        assert checker.is_internal_url("/relative/path")
        assert checker.is_internal_url("relative/path")
        assert checker.is_internal_url("#fragment")

        # External URLs
        assert not checker.is_internal_url("http://other.com/page")
        assert not checker.is_internal_url("https://external.site")

    def test_normalize_url(self):
        """Test URL normalization."""
        checker = LinkChecker("http://example.com")

        # Remove fragments
        assert checker.normalize_url("http://example.com/page#section") == "http://example.com/page"

        # Remove index.html from directories
        assert (
            checker.normalize_url("http://example.com/docs/index.html")
            == "http://example.com/docs/"
        )

        # Keep regular files
        assert (
            checker.normalize_url("http://example.com/page.html") == "http://example.com/page.html"
        )

    def test_extract_links_from_html(self):
        """Test link extraction from HTML content."""
        checker = LinkChecker("http://example.com")

        html_content = """
        <html>
            <head>
                <link rel="stylesheet" href="/styles.css">
            </head>
            <body>
                <a href="/page1.html">Page 1</a>
                <a href="http://external.com">External</a>
                <a href="mailto:test@example.com">Email</a>
                <a href="javascript:void(0)">JS Link</a>
                <img src="/image.jpg" alt="Image">
                <script src="/script.js"></script>
                <img src="data:image/png;base64,abc123" alt="Data URL">
            </body>
        </html>
        """

        links = checker.extract_links_from_html(html_content, "http://example.com")

        expected_links = {
            "http://example.com/page1.html",
            "http://external.com",
            "http://example.com/image.jpg",
            "http://example.com/script.js",
            "http://example.com/styles.css",
        }

        assert links == expected_links

    def test_extract_relative_links_from_origin_without_trailing_slash(self):
        """Treat a bare origin as the site root when resolving relative links."""
        checker = LinkChecker("http://127.0.0.1:8000/")

        links = checker.extract_links_from_html(
            '<a href="docs/">Docs</a><link href="assets/site.webmanifest">',
            "http://127.0.0.1:8000",
        )

        assert links == {
            "http://127.0.0.1:8000/docs/",
            "http://127.0.0.1:8000/assets/site.webmanifest",
        }

    @responses.activate
    def test_check_url_success(self):
        """Test successful URL checking."""
        checker = LinkChecker("http://example.com")

        responses.add(responses.HEAD, "http://example.com/page", status=200)

        status_code, reason = checker.check_url("http://example.com/page")
        assert status_code == 200
        assert reason == "OK"

    @responses.activate
    def test_check_url_method_not_allowed_fallback(self):
        """Test fallback to GET when HEAD returns 405."""
        checker = LinkChecker("http://example.com")

        responses.add(responses.HEAD, "http://example.com/page", status=405)
        responses.add(responses.GET, "http://example.com/page", status=200)

        status_code, reason = checker.check_url("http://example.com/page")
        assert status_code == 200

    @responses.activate
    def test_check_url_not_found(self):
        """Test URL checking for 404 errors."""
        checker = LinkChecker("http://example.com")

        responses.add(responses.HEAD, "http://example.com/missing", status=404)

        status_code, reason = checker.check_url("http://example.com/missing")
        assert status_code == 404
        assert reason == "Not Found"

    def test_check_url_connection_error(self):
        """Test URL checking with connection errors."""
        checker = LinkChecker("http://example.com")

        # Mock a connection error
        with patch.object(
            checker.session,
            "head",
            side_effect=requests.exceptions.ConnectionError("Connection failed"),
        ):
            status_code, reason = checker.check_url("http://example.com/page")
            assert status_code is None
            assert "Connection failed" in reason

    @responses.activate
    def test_crawl_page_basic(self):
        """Test basic page crawling."""
        checker = LinkChecker("http://example.com")

        html_content = """
        <html>
            <body>
                <a href="/page2.html">Page 2</a>
                <a href="http://external.com">External</a>
            </body>
        </html>
        """

        responses.add(responses.GET, "http://example.com/", body=html_content, status=200)
        responses.add(responses.HEAD, "http://example.com/page2.html", status=200)
        responses.add(responses.HEAD, "http://external.com", status=200)

        checker.crawl_page("http://example.com/")

        assert "http://example.com/" in checker.visited_urls
        assert "http://example.com/page2.html" in checker.checked_links
        assert "http://external.com" in checker.checked_links
        assert len(checker.dead_links) == 0

    @responses.activate
    def test_crawl_page_with_broken_links(self):
        """Test crawling page with broken links."""
        checker = LinkChecker("http://example.com")

        html_content = """
        <html>
            <body>
                <a href="/working.html">Working</a>
                <a href="/broken.html">Broken</a>
            </body>
        </html>
        """

        responses.add(responses.GET, "http://example.com/", body=html_content, status=200)
        responses.add(responses.HEAD, "http://example.com/working.html", status=200)
        responses.add(responses.HEAD, "http://example.com/broken.html", status=404)

        checker.crawl_page("http://example.com/")

        assert len(checker.dead_links) == 1
        assert checker.dead_links[0]["url"] == "http://example.com/broken.html"
        assert checker.dead_links[0]["status"] == 404
        assert checker.dead_links[0]["found_on"] == "http://example.com/"

    @responses.activate
    def test_crawl_page_max_depth(self):
        """Test that crawling respects max depth."""
        checker = LinkChecker("http://example.com", max_depth=1)

        # Page 1 content
        page1_content = '<a href="/page2.html">Page 2</a>'
        responses.add(responses.GET, "http://example.com/", body=page1_content, status=200)
        responses.add(responses.HEAD, "http://example.com/page2.html", status=200)

        # Page 2 content (should not be crawled due to depth limit)
        page2_content = '<a href="/page3.html">Page 3</a>'
        responses.add(
            responses.GET,
            "http://example.com/page2.html",
            body=page2_content,
            status=200,
        )
        responses.add(responses.HEAD, "http://example.com/page3.html", status=200)

        checker.crawl_page("http://example.com/")

        # Should visit page1 and page2, but not page3
        assert "http://example.com/" in checker.visited_urls
        assert "http://example.com/page2.html" in checker.visited_urls
        assert len(checker.visited_urls) == 2

    @responses.activate
    def test_crawl_page_avoids_duplicates(self):
        """Test that crawling avoids visiting the same page twice."""
        checker = LinkChecker("http://example.com")

        html_content = '<a href="/">Home</a>'
        responses.add(responses.GET, "http://example.com/", body=html_content, status=200)
        responses.add(responses.HEAD, "http://example.com/", status=200)

        # Crawl the same page twice
        checker.crawl_page("http://example.com/")
        initial_visit_count = len(checker.visited_urls)

        checker.crawl_page("http://example.com/")
        final_visit_count = len(checker.visited_urls)

        assert initial_visit_count == final_visit_count == 1

    @responses.activate
    def test_crawl_page_handles_non_200_response(self):
        """Test crawling handles non-200 responses gracefully."""
        checker = LinkChecker("http://example.com")

        responses.add(responses.GET, "http://example.com/", status=500)

        checker.crawl_page("http://example.com/")

        assert "http://example.com/" in checker.visited_urls
        assert len(checker.checked_links) == 0  # No links to check if page failed

    def test_crawl_page_handles_request_exception(self):
        """Test crawling handles request exceptions gracefully."""
        checker = LinkChecker("http://example.com")

        with patch.object(
            checker.session,
            "get",
            side_effect=requests.exceptions.RequestException("Network error"),
        ):
            checker.crawl_page("http://example.com/")

            assert "http://example.com/" in checker.visited_urls
            assert len(checker.checked_links) == 0

    def test_no_crawl_trees_are_entered_but_never_descended(self):
        """The build writes each tree's entry page; only what sits below is opaque."""
        checker = LinkChecker("http://example.com")
        assert checker.is_crawlable_url("http://example.com/docs/index.html")
        assert checker.is_crawlable_url("http://example.com/coverage")
        assert checker.is_crawlable_url("http://example.com/coverage/")
        assert not checker.is_crawlable_url("http://example.com/coverage/website/z_a_py.html")
        # Only whole path segments match, so a similarly named page still crawls.
        assert checker.is_crawlable_url("http://example.com/coverage-policy.html")
        # Prefixes resolve against the served site root, not the URL host.
        nested = LinkChecker("http://example.com/site/docs")
        assert not nested.is_crawlable_url("http://example.com/site/coverage/website/")
        assert nested.is_crawlable_url("http://example.com/site/docs/index.html")
        # Callers may name their own trees, or opt out of the behavior entirely.
        named = LinkChecker("http://example.com", no_crawl=("reports",))
        assert not named.is_crawlable_url("http://example.com/reports/website/")
        assert named.is_crawlable_url("http://example.com/coverage/website/")
        opted_out = LinkChecker("http://example.com", no_crawl=())
        assert opted_out.is_crawlable_url("http://example.com/coverage/website/")

    @responses.activate
    def test_coverage_report_source_is_never_parsed_for_links(self):
        """coverage.py renders the builder's own hrefs; they must not be followed."""
        checker = LinkChecker("http://example.com", max_depth=5)

        index = '<a href="/coverage/">Coverage</a>'
        cards = '<a href="website/">View Detailed Report</a>'
        # The literal href= text coverage.py emits when highlighting our source.
        source = '<a href="loader/">View Detailed Report</a>'
        responses.add(responses.GET, "http://example.com/", body=index, status=200)
        responses.add(responses.HEAD, "http://example.com/coverage/", status=200)
        responses.add(responses.GET, "http://example.com/coverage/", body=cards, status=200)
        responses.add(responses.HEAD, "http://example.com/coverage/website/", status=200)
        responses.add(
            responses.GET, "http://example.com/coverage/website/", body=source, status=200
        )

        checker.crawl_page("http://example.com/")

        # The index is crawled, so a dead card link would still be caught...
        assert "http://example.com/coverage/" in checker.visited_urls
        assert "http://example.com/coverage/website/" in checker.checked_links
        # ...but the generated report below it is never opened.
        assert "http://example.com/coverage/website/" not in checker.visited_urls
        assert checker.dead_links == []

    @responses.activate
    def test_links_are_visited_in_sorted_order(self):
        """Traversal must not depend on the order links come out of the set.

        First discovery decides a page's depth, and str hashing is randomized
        per process, so an unsorted walk makes the check flaky between runs.
        """
        checker = LinkChecker("http://example.com", max_depth=1)

        responses.add(responses.GET, "http://example.com/", body="", status=200)
        for name in ("a", "b", "c"):
            responses.add(responses.HEAD, f"http://example.com/{name}.html", status=404)

        # Hand the crawler its links in reverse order; it must still walk sorted.
        reversed_links = [f"http://example.com/{name}.html" for name in ("c", "b", "a")]
        with patch.object(checker, "extract_links_from_html", return_value=reversed_links):
            checker.crawl_page("http://example.com/")

        assert [link["url"] for link in checker.dead_links] == sorted(reversed_links)
