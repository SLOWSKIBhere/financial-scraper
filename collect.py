import asyncio
import json
import logging
import os
import random
import re
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Dict, List, Optional, Tuple

import feedparser
import httpx
from pydantic import BaseModel, Field, ValidationError

# Calculate script base directory to guarantee permission resilience
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_FILE_PATH = os.path.join(SCRIPT_DIR, "scraper.log")
CONFIG_FILE_PATH = os.path.join(SCRIPT_DIR, "config.json")
OUTPUT_FILE_PATH = os.path.join(SCRIPT_DIR, "financial_report.json")
METRICS_FILE_PATH = os.path.join(SCRIPT_DIR, "collect_metrics.json")

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_FILE_PATH, mode="a", encoding="utf-8")
    ]
)
logger = logging.getLogger("AutonomousResilientScraper")

# Define Pydantic Schemas matching strict rules
class NewsArticle(BaseModel):
    title: str = Field(..., min_length=1, description="The verified title of the news article")
    summary: Optional[str] = Field(default=None, description="The description or summary of the article")
    url: str = Field(..., description="The verified HTTP/HTTPS URL of the article")
    published: Optional[str] = Field(default=None, description="ISO 8601 formatted datetime string, or null if unknown")
    fetched_at: str = Field(..., description="ISO 8601 timestamp when the article was collected")
    source: str = Field(..., min_length=1, description="The name of the source website")

class SourceResult(BaseModel):
    source: str
    articles: List[NewsArticle]

class FinalOutput(BaseModel):
    results: List[SourceResult]
    errors: List[str] = []

# List of common realistic User-Agents to prevent bot detection
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
]

def clean_html(raw_html: str) -> str:
    """Helper to remove HTML tags and decode basic entities from descriptions/titles."""
    if not raw_html:
        return ""
    clean = re.sub(r'<[^>]+>', '', raw_html)
    clean = clean.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"').replace("&#39;", "'")
    return clean.strip()

def parsed_struct_to_iso(parsed_time: Optional[time.struct_time]) -> Optional[str]:
    """Convert feedparser *_parsed struct_time to ISO 8601."""
    if not parsed_time:
        return None
    try:
        dt = datetime(*parsed_time[:6], tzinfo=timezone.utc)
        return dt.isoformat()
    except (TypeError, ValueError, OverflowError):
        return None

def entry_published_iso(entry: Any) -> Optional[str]:
    """Resolve publication date from feed entry fields; null if absent."""
    iso = parsed_struct_to_iso(entry.get("published_parsed"))
    if iso:
        return iso
    iso = parsed_struct_to_iso(entry.get("updated_parsed"))
    if iso:
        return iso
    for field in ("published", "updated"):
        raw = entry.get(field)
        if not raw:
            continue
        try:
            dt = parsedate_to_datetime(raw)
            return dt.isoformat()
        except (TypeError, ValueError, OverflowError):
            try:
                date_clean = raw.replace("Z", "+00:00")
                dt = datetime.fromisoformat(date_clean)
                return dt.isoformat()
            except (TypeError, ValueError):
                continue
    return None

def entry_summary(entry: Any) -> Optional[str]:
    """Extract summary/description/content from a feedparser entry."""
    raw = entry.get("summary") or entry.get("description")
    if not raw and entry.get("content"):
        parts = []
        for block in entry.content:
            if isinstance(block, dict) and block.get("value"):
                parts.append(block["value"])
        if parts:
            raw = " ".join(parts)
    if raw:
        cleaned = clean_html(raw)
        return cleaned if cleaned else None
    return None

def entry_url(entry: Any) -> str:
    """Resolve article URL from feedparser entry."""
    link = entry.get("link")
    if link:
        return link.strip()
    for link_item in entry.get("links") or []:
        if isinstance(link_item, dict) and link_item.get("href"):
            return link_item["href"].strip()
    return ""

class ResilientScraper:
    def __init__(self, config_path: str = CONFIG_FILE_PATH):
        self.config_path = config_path
        self.sources = []
        self.semaphore = asyncio.Semaphore(3)
        self.source_metrics: List[Dict[str, Any]] = []
        self.load_config()

    def load_config(self):
        try:
            with open(self.config_path, "r", encoding="utf-8") as f:
                config_data = json.load(f)
                self.sources = config_data.get("approved_sources", [])
                logger.info(f"Loaded {len(self.sources)} sources from configuration.")
        except Exception as e:
            logger.error(f"Failed to load config: {e}")
            raise

    async def fetch_with_backoff(self, client: httpx.AsyncClient, name: str, url: str) -> Tuple[Optional[str], Optional[str]]:
        """Fetch URL content with exponential backoff. Returns (body, error_message)."""
        max_retries = 3
        base_delay = 2.0

        for attempt in range(max_retries):
            headers = {
                "User-Agent": random.choice(USER_AGENTS),
                "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*;q=0.8",
                "Accept-Language": "en-US,en;q=0.5"
            }
            try:
                async with self.semaphore:
                    logger.info(f"Fetching {name} (Attempt {attempt + 1}/{max_retries})...")
                    response = await client.get(url, headers=headers, timeout=12.0, follow_redirects=True)

                if response.status_code == 200:
                    return response.text, None
                if response.status_code in (429, 503):
                    delay = (base_delay ** attempt) + random.uniform(0.5, 1.5)
                    logger.warning(f"Rate limited (Status {response.status_code}) on {name}. Backing off for {delay:.2f}s...")
                    await asyncio.sleep(delay)
                else:
                    err = f"HTTP {response.status_code} on {name}"
                    logger.error(f"{err}. Non-retryable.")
                    return None, err
            except (httpx.RequestError, asyncio.TimeoutError) as e:
                delay = (base_delay ** attempt) + random.uniform(0.5, 1.5)
                logger.warning(f"Request error on {name}: {e}. Retrying in {delay:.2f}s...")
                await asyncio.sleep(delay)

        err = f"Failed to fetch {name} after {max_retries} attempts"
        logger.error(err)
        return None, err

    def parse_feed_content(self, feed_body: str, source_name: str, fetched_at: str) -> Tuple[List[dict], Optional[str]]:
        """Parse RSS/Atom with feedparser into structured dicts."""
        articles: List[dict] = []
        if not feed_body:
            return articles, "Empty feed body"

        parsed = feedparser.parse(feed_body)
        if getattr(parsed, "bozo", False) and not parsed.entries:
            bozo_exc = getattr(parsed, "bozo_exception", None)
            return articles, f"Feed parse error: {bozo_exc}"

        for entry in parsed.entries:
            title = clean_html(entry.get("title") or "")
            url = entry_url(entry)
            summary = entry_summary(entry)
            published = entry_published_iso(entry)

            if title and url:
                articles.append({
                    "title": title,
                    "summary": summary,
                    "url": url,
                    "published": published,
                    "fetched_at": fetched_at,
                    "source": source_name,
                })

        if not articles and parsed.feed:
            return articles, "Feed contained no valid entries"
        return articles, None

    def _empty_metrics(self, name: str) -> Dict[str, Any]:
        return {
            "source": name,
            "success": False,
            "article_count": 0,
            "summaries_present": 0,
            "missing_dates": 0,
            "errors": [],
        }

    async def scrape_source(self, client: httpx.AsyncClient, source: dict) -> tuple:
        """Scrape a single approved RSS/Atom source and return results plus metrics."""
        name = source["name"]
        url = source["url"]
        fetched_at = datetime.now(timezone.utc).isoformat()
        metrics = self._empty_metrics(name)

        content, fetch_err = await self.fetch_with_backoff(client, name, url)
        if not content:
            err = fetch_err or f"Source '{name}' fetch failed completely (all retries exhausted)"
            metrics["errors"].append(err)
            return name, None, err, metrics

        try:
            raw_articles, parse_err = self.parse_feed_content(content, name, fetched_at)
            if parse_err:
                metrics["errors"].append(parse_err)
            if not raw_articles:
                err = parse_err or f"Source '{name}' returned no articles"
                if err not in metrics["errors"]:
                    metrics["errors"].append(err)
                return name, None, err, metrics

            valid_articles = []
            validation_errors = 0

            for raw in raw_articles:
                try:
                    article = NewsArticle(**raw)
                    valid_articles.append(article)
                except ValidationError as ve:
                    validation_errors += 1
                    logger.debug(f"Pydantic Validation failed for an article in {name}: {ve}")
                    continue

            metrics["article_count"] = len(valid_articles)
            metrics["summaries_present"] = sum(1 for a in valid_articles if a.summary)
            metrics["missing_dates"] = sum(1 for a in valid_articles if a.published is None)
            metrics["success"] = len(valid_articles) > 0

            logger.info(
                f"Successfully processed {name}: {len(valid_articles)} validated articles "
                f"({validation_errors} malformed skipped)."
            )
            if not valid_articles:
                err = f"All articles from '{name}' failed validation checks"
                metrics["errors"].append(err)
                return name, None, err, metrics

            return name, SourceResult(source=name, articles=valid_articles), None, metrics

        except Exception as e:
            logger.error(f"Error scraping source {name}: {e}")
            err = f"Unexpected error during parsing of '{name}': {str(e)}"
            metrics["errors"].append(err)
            return name, None, err, metrics

    def write_metrics(self, total_articles: int, errors_list: List[str]) -> None:
        """Write per-source collection metrics to JSON."""
        payload = {
            "collected_at": datetime.now(timezone.utc).isoformat(),
            "total_articles": total_articles,
            "sources": self.source_metrics,
            "global_errors": errors_list,
        }
        try:
            with open(METRICS_FILE_PATH, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)
            logger.info(f"Collection metrics saved to {METRICS_FILE_PATH}.")
        except Exception as e:
            logger.critical(f"Failed to write metrics file: {e}")

    async def run_pipeline(self) -> dict:
        """Run the full asynchronous pipeline to scrape all sources and output schema-valid JSON."""
        logger.info("Starting autonomous financial news collection pipeline...")

        limits = httpx.Limits(max_keepalive_connections=5, max_connections=10)
        async with httpx.AsyncClient(limits=limits, verify=False) as client:
            tasks = [self.scrape_source(client, src) for src in self.sources]
            results = await asyncio.gather(*tasks)

        final_results = []
        errors_list = []
        self.source_metrics = []
        total_articles = 0

        for name, source_res, err, metrics in results:
            self.source_metrics.append(metrics)
            if err:
                logger.error(err)
                errors_list.append(err)
            elif source_res:
                final_results.append(source_res)
                total_articles += len(source_res.articles)

        output_data = FinalOutput(results=final_results, errors=errors_list)

        try:
            with open(OUTPUT_FILE_PATH, "w", encoding="utf-8") as f:
                f.write(output_data.model_dump_json(indent=2))
            logger.info(f"Successfully compiled all facts. Results saved to {OUTPUT_FILE_PATH}.")
        except Exception as e:
            logger.critical(f"Failed to write final output to file: {e}")
            errors_list.append(f"Write failure: {str(e)}")

        self.write_metrics(total_articles, errors_list)
        return output_data.model_dump()

if __name__ == "__main__":
    try:
        import google.antigravity
        logger.info("Google Antigravity SDK detected! Programmatic runtime active.")
    except ImportError:
        logger.info("Google Antigravity SDK not installed. Running under local resilient runtime framework.")

    loop_output = asyncio.run(ResilientScraper().run_pipeline())
    print("\n--- PIPELINE EXECUTION OUTPUT ---\n")
    print(json.dumps(loop_output, indent=2))
