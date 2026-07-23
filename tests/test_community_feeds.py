import importlib
import logging
import sys
import types
import unittest
from unittest.mock import patch

with patch.object(logging, "FileHandler", lambda *args, **kwargs: logging.NullHandler()):
    community_feeds = importlib.import_module("community_feeds")


class FakeResponse:
    text = "<rss />"

    def raise_for_status(self):
        return None


class FakeClient:
    def get(self, url, timeout):
        return FakeResponse()



class CommunityFeedsTests(unittest.TestCase):
    def test_category_matching_requires_keyword_boundaries(self):
        cases = (
            ("method", "", "markets", "markets"),
            ("space exploration", "", "markets", "markets"),
            ("ETH/USD rallies", "", "markets", "crypto"),
            ("SPAC merger announced", "", "markets", "ipo"),
            ("Neutral headline", "FED meeting expected", "markets", "policy"),
        )
        for title, summary, default, expected in cases:
            with self.subTest(title=title, summary=summary):
                self.assertEqual(
                    community_feeds.classify_category(title, summary, default),
                    expected,
                )

    def test_malformed_article_is_not_committed_to_seen_urls(self):
        bad_url = "https://example.test/bad"
        good_url = "https://example.test/good"
        feed = types.SimpleNamespace(entries=[
            types.SimpleNamespace(link=bad_url, title="Valid title", summary=object()),
            types.SimpleNamespace(link=good_url, title="Good title", summary="Summary"),
        ])
        scraper = community_feeds.CommunityFeedsScraper()
        scraper.seen_urls = set()

        with patch.object(community_feeds.feedparser, "parse", return_value=feed):
            result = scraper.fetch_source(
                FakeClient(), "Example", "https://example.test/feed", "markets"
            )

        self.assertEqual(result.article_count, 1)
        self.assertNotIn(community_feeds.url_hash(bad_url), scraper.seen_urls)
        self.assertIn(community_feeds.url_hash(good_url), scraper.seen_urls)


if __name__ == "__main__":
    unittest.main()
