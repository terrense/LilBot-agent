from __future__ import annotations

import unittest

from lilbot.tools.builtin import _parse_bing_results, _parse_duckduckgo_results, _validate_public_url


class WebToolTests(unittest.TestCase):
    def test_parse_duckduckgo_results_decodes_redirects(self):
        html = """
        <a class="result__a" href="/l/?uddg=https%3A%2F%2Fexample.com%2Fdocs">Example Docs</a>
        <div class="result__snippet">A short <b>snippet</b>.</div>
        """
        results = _parse_duckduckgo_results(html, 5)
        self.assertEqual(results[0]["title"], "Example Docs")
        self.assertEqual(results[0]["url"], "https://example.com/docs")
        self.assertEqual(results[0]["snippet"], "A short snippet.")

    def test_parse_bing_results_extracts_title_url_and_snippet(self):
        html = """
        <li class="b_algo">
          <h2><a href="https://example.org/page">Example Page</a></h2>
          <div class="b_caption"><p>Readable search snippet.</p></div>
        </li>
        """
        results = _parse_bing_results(html, 5)
        self.assertEqual(results[0]["title"], "Example Page")
        self.assertEqual(results[0]["url"], "https://example.org/page")
        self.assertEqual(results[0]["snippet"], "Readable search snippet.")

    def test_fetch_url_rejects_local_and_non_http_targets(self):
        self.assertIn("localhost", _validate_public_url("http://localhost:8080"))
        self.assertIn("https", _validate_public_url("file:///C:/Windows/win.ini"))
        self.assertIn("restricted", _validate_public_url("http://127.0.0.1"))
        self.assertIn("restricted", _validate_public_url("http://198.18.0.5"))


if __name__ == "__main__":
    unittest.main()
