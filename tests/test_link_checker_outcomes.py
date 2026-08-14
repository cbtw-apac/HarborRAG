"""High-level and edge-case tests for the website link checker."""

from unittest.mock import patch

import requests
import responses
from website.check_links import LinkChecker


class TestLinkCheckerOutcomes:
    """Test complete runs and non-standard link-checking outcomes."""

    @responses.activate
    def test_run_check_success(self):
        """Test complete link check run with no broken links."""
        checker = LinkChecker("http://example.com", max_depth=1)

        html_content = '<a href="/page.html">Page</a>'
        responses.add(responses.GET, "http://example.com", body=html_content, status=200)
        responses.add(responses.HEAD, "http://example.com/page.html", status=200)

        success = checker.run_check()

        assert success is True
        assert len(checker.dead_links) == 0

    @responses.activate
    def test_run_check_with_broken_links(self):
        """Test complete link check run with broken links."""
        checker = LinkChecker("http://example.com", max_depth=1)

        html_content = '<a href="/broken.html">Broken</a>'
        responses.add(responses.GET, "http://example.com", body=html_content, status=200)
        responses.add(responses.HEAD, "http://example.com/broken.html", status=404)

        success = checker.run_check()

        assert success is False
        assert len(checker.dead_links) == 1

    @responses.activate
    def test_redirects_handling(self):
        """Test handling of redirect responses."""
        checker = LinkChecker("http://example.com")

        html_content = '<a href="/redirect">Redirect</a>'
        responses.add(responses.GET, "http://example.com/", body=html_content, status=200)
        responses.add(responses.HEAD, "http://example.com/redirect", status=301)

        checker.crawl_page("http://example.com/")

        # Redirects should not be considered broken
        assert len(checker.dead_links) == 0

    def test_link_extraction_edge_cases(self):
        """Test link extraction with edge cases."""
        checker = LinkChecker("http://example.com")

        html_content = """
        <html>
            <body>
                <a href="">Empty href</a>
                <a href="#fragment-only">Fragment only</a>
                <a href="   /spaced.html   ">Spaced URL</a>
                <img src="">Empty src</a>
            </body>
        </html>
        """

        links = checker.extract_links_from_html(html_content, "http://example.com")

        # Should handle edge cases gracefully
        assert isinstance(links, set)

    @responses.activate
    def test_timeout_handling(self):
        """Test handling of request timeouts."""
        checker = LinkChecker("http://example.com")

        with patch.object(
            checker.session,
            "head",
            side_effect=requests.exceptions.Timeout("Request timed out"),
        ):
            status_code, reason = checker.check_url("http://example.com/slow")

            assert status_code is None
            assert "Request timed out" in reason
