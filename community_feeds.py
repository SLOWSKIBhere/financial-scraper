"""
community_feeds.py — Community & Market Sentiment RSS Scraper (replaces reddit_collect.py)

Reddit's JSON API blocks all server/cloud IPs (403 on all endpoints).
This module replaces it with 5 working high-quality alternative sources:
  - Financial Times Markets (premium market analysis)
  - WSJ Market Pulse (Wall Street Journal)
  - Seeking Alpha (investor community analysis)
  - CoinDesk (crypto market news)
  - CoinTelegraph (crypto community sentiment)

Output format is identical to the original reddit_collect.py FinalOutput schema
so --merge into financial_report.json works unchanged.

Usage:
    python community_feeds.py
    python community_feeds.py --merge

Guardrails:
- No imaginary libraries — httpx, feedparser, pydantic v2, asyncio only
- No placeholders — all logic fully implemented
- Every function commented in plain English
- Respects rate limits: 0.5s delay between requests
"""

import asyncio
import json
import logging
import os
import time
import hashlib
import argparse
from datetime import datetime, timezone
from typing import List, Optional, Tuple

import httpx
import feedparser
from pydantic import BaseModel, Field, ValidationError

# ─── Paths ────────────────────────────────────────────────────────────────────
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_FILE_PATH  = os.path.join(SCRIPT_DIR, "community_feeds.log")
OUTPUT_FILE_PATH  = os.path.join(SCRIPT_DIR, "community_report.json")
MERGED_OUTPUT_PATH = os.path.join(SCRIPT_DIR, "financial_report.json")
SEEN_URLS_PATH = os.path.join(SCRIPT_DIR, "seen_urls.json")

# ─── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_FILE_PATH, mode="a", encoding="utf-8"),
    ],
)
logger = logging.getLogger("CommunityFeedsScraper")

# ─── Sources ──────────────────────────────────────────────────────────────────
# Each tuple: (display_name, rss_url, category_tag)
COMMUNITY_SOURCES: List[Tuple[str, str, str]] = [
    ("FT Markets",     "https://www.ft.com/markets?format=rss",                              "macro"),
    ("WSJ Markets",    "https://feeds.content.dowjones.io/public/rss/mw_marketpulse",        "markets"),
    ("Seeking Alpha",  "https://seekingalpha.com/feed.xml",                                  "markets"),
    ("CoinDesk",       "https://www.coindesk.com/arc/outboundfeeds/rss/",                    "crypto"),
    ("CoinTelegraph",  "https://cointelegraph.com/rss",                                      "crypto"),
]

# Keywords for lightweight category override (supplements source default)
CATEGORY_KEYWORDS = {
    "crypto":       ["bitcoin", "ethereum", "crypto", "blockchain", "defi", "nft", "token", "btc", "eth"],
    "earnings":     ["earnings", "revenue", "profit", "eps", "quarterly", "guidance", "beat", "miss"],
    "ipo":          ["ipo", "listing", "debut", "offering", "spac", "went public"],
    "policy":       ["fed", "federal reserve", "interest rate", "inflation", "tariff", "regulation", "sec", "fomc"],
    "commodities":  ["oil", "gold", "silver", "commodity", "crude", "natural gas", "wheat", "corn"],
    "macro":        ["gdp", "unemployment", "jobs", "economy", "recession", "growth", "trade deficit"],
}

# ─── Pydantic Models (identical schema to collect_v2.py) ──────────────────────
class NewsArticle(BaseModel):
    title: str
    summary: Optional[str] = None
    url: str
    published: Optional[str] = None
    fetched_at: str
    source: str
    category: str = "markets"

class SourceResult(BaseModel):
    source: str
    articles: List[NewsArticle] = Field(default_factory=list)
    error: Optional[str] = None
    article_count: int = 0

class FinalOutput(BaseModel):
    collected_at: str
    total_articles: int
    results: List[SourceResult]


# ─── Helpers ──────────────────────────────────────────────────────────────────
def load_seen_urls() -> set:
    """Load the dedup set of already-seen URLs from seen_urls.json."""
    if os.path.exists(SEEN_URLS_PATH):
        try:
            with open(SEEN_URLS_PATH, "r", encoding="utf-8") as f:
                return set(json.load(f))
        except Exception:
            return set()
    return set()


def save_seen_urls(seen: set) -> None:
    """Persist the dedup URL set back to disk."""
    with open(SEEN_URLS_PATH, "w", encoding="utf-8") as f:
        json.dump(list(seen), f)


def classify_category(title: str, summary: str, default: str) -> str:
    """
    Classify an article into a category using keyword matching.
    Falls back to the source-level default if no keywords match.
    Checks title + summary (lowercased) against CATEGORY_KEYWORDS dict.
    """
    text = (title + " " + (summary or "")).lower()
    for category, keywords in CATEGORY_KEYWORDS.items():
        if any(kw in text for kw in keywords):
            return category
    return default


def parse_published(entry) -> Optional[str]:
    """
    Extract and normalize the published date from a feedparser entry.
    Returns ISO 8601 UTC string or None if unparseable.
    """
    try:
        if hasattr(entry, "published_parsed") and entry.published_parsed:
            import calendar
            ts = calendar.timegm(entry.published_parsed)
            return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
    except Exception:
        pass
    return None


def url_hash(url: str) -> str:
    """SHA-256 hash of a URL — used as the dedup key in seen_urls.json."""
    return hashlib.sha256(url.encode()).hexdigest()


# ─── Scraper ──────────────────────────────────────────────────────────────────
class CommunityFeedsScraper:
    """
    Fetches RSS feeds from community/market sentiment sources.
    Deduplicates against seen_urls.json (shared with collect_v2.py).
    """

    def __init__(self):
        self.headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept": "application/rss+xml, application/xml, text/xml, */*",
        }
        self.seen_urls = load_seen_urls()

    def fetch_source(
        self,
        client: httpx.Client,
        name: str,
        url: str,
        default_category: str,
    ) -> SourceResult:
        """
        Fetch one RSS source synchronously.
        Parses entries, deduplicates, classifies categories.
        Returns a SourceResult with all new articles.
        """
        try:
            logger.info(f"Fetching {name}...")
            response = client.get(url, timeout=12)
            response.raise_for_status()

            feed = feedparser.parse(response.text)
            articles: List[NewsArticle] = []
            fetched_at = datetime.now(timezone.utc).isoformat()

            for entry in feed.entries[:50]:  # cap per source
                article_url = getattr(entry, "link", "")
                if not article_url or not article_url.startswith("https://"):
                    continue

                # Dedup check
                uhash = url_hash(article_url)
                if uhash in self.seen_urls:
                    continue
                self.seen_urls.add(uhash)

                title = getattr(entry, "title", "").replace("\n", " ").replace("\r", " ").strip()
                summary = getattr(entry, "summary", None)
                if summary:
                    summary = summary[:500].replace("\n", " ").strip()

                category = classify_category(title, summary or "", default_category)

                try:
                    articles.append(NewsArticle(
                        title=title,
                        summary=summary,
                        url=article_url,
                        published=parse_published(entry),
                        fetched_at=fetched_at,
                        source=name,
                        category=category,
                    ))
                except ValidationError as ve:
                    logger.warning(f"Validation error on {name}: {ve}")
                    continue

            logger.info(f"{name}: {len(articles)} new articles")
            return SourceResult(source=name, articles=articles, article_count=len(articles))

        except Exception as e:
            logger.error(f"{name} failed: {e}")
            return SourceResult(source=name, error=str(e), article_count=0)

    def run(self) -> FinalOutput:
        """
        Scrape all COMMUNITY_SOURCES sequentially (0.5s delay between requests).
        Returns a FinalOutput with results from all sources.
        """
        results: List[SourceResult] = []

        with httpx.Client(headers=self.headers, follow_redirects=True) as client:
            for name, url, category in COMMUNITY_SOURCES:
                result = self.fetch_source(client, name, url, category)
                results.append(result)
                time.sleep(0.5)  # polite rate limiting

        save_seen_urls(self.seen_urls)

        total = sum(r.article_count for r in results)
        logger.info(f"Community feeds complete — {total} new articles across {len(results)} sources")

        return FinalOutput(
            collected_at=datetime.now(timezone.utc).isoformat(),
            total_articles=total,
            results=results,
        )


def merge_into_financial_report(output: FinalOutput) -> None:
    """
    Merge community feed results into financial_report.json.
    Appends new articles — does not overwrite existing RSS data.
    """
    existing: dict = {}
    if os.path.exists(MERGED_OUTPUT_PATH):
        try:
            with open(MERGED_OUTPUT_PATH, "r", encoding="utf-8") as f:
                existing = json.load(f)
        except Exception:
            existing = {}

    # financial_report.json stores: {results: [{source, articles: [...]}]}
    existing_results: List[dict] = existing.get("results", [])
    existing_sources = {r["source"]: r for r in existing_results}

    for result in output.results:
        if result.article_count == 0:
            continue
        new_articles = [a.model_dump() for a in result.articles]
        if result.source in existing_sources:
            existing_sources[result.source]["articles"].extend(new_articles)
        else:
            existing_sources[result.source] = result.model_dump()

    existing["results"] = list(existing_sources.values())
    existing["updated_at"] = datetime.now(timezone.utc).isoformat()
    existing["total_articles"] = sum(
        len(r.get("articles", [])) for r in existing["results"]
    )

    with open(MERGED_OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(existing, f, indent=2, ensure_ascii=False)

    logger.info(f"Merged into {MERGED_OUTPUT_PATH} — total {existing['total_articles']} articles")


def main():
    parser = argparse.ArgumentParser(description="Community & market sentiment RSS scraper")
    parser.add_argument("--merge", action="store_true", help="Merge results into financial_report.json")
    args = parser.parse_args()

    scraper = CommunityFeedsScraper()
    output = scraper.run()

    # Always write standalone community report
    with open(OUTPUT_FILE_PATH, "w", encoding="utf-8") as f:
        json.dump(output.model_dump(), f, indent=2, ensure_ascii=False)

    total = output.total_articles
    logger.info(f"Community report saved to {OUTPUT_FILE_PATH} — {total} articles")

    if args.merge:
        merge_into_financial_report(output)

    # Print summary
    print("\n" + "=" * 50)
    print("COMMUNITY FEEDS SUMMARY")
    print("=" * 50)
    for r in output.results:
        status = f"{r.article_count} articles" if not r.error else f"ERROR: {r.error[:50]}"
        print(f"  {r.source}: {status}")
    print(f"\nTotal new articles: {total}")
    print("=" * 50)


if __name__ == "__main__":
    main()
