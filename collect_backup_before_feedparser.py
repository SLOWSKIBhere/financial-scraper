import asyncio
import json
import logging
import os
import random
import re
import xml.etree.ElementTree as ET
from datetime import datetime
from email.utils import parsedate_to_datetime
from typing import List, Optional
import httpx
from pydantic import BaseModel, Field, ValidationError

# Calculate script base directory to guarantee permission resilience
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_FILE_PATH = os.path.join(SCRIPT_DIR, "scraper.log")
CONFIG_FILE_PATH = os.path.join(SCRIPT_DIR, "config.json")
OUTPUT_FILE_PATH = os.path.join(SCRIPT_DIR, "financial_report.json")

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
    published: str = Field(..., description="ISO 8601 formatted datetime string")
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
    # Remove HTML tags
    clean = re.sub(r'<[^>]+>', '', raw_html)
    # Basic unescaping
    clean = clean.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"').replace("&#39;", "'")
    return clean.strip()

def parse_rss_date(date_str: str) -> str:
    """Convert typical RSS (RFC 822) or Atom (ISO 8601) dates into ISO format."""
    if not date_str:
        return datetime.utcnow().isoformat() + "Z"
    try:
        # Try RFC 822 format (e.g. "Mon, 25 May 2026 10:26:00 GMT")
        dt = parsedate_to_datetime(date_str)
        return dt.isoformat()
    except Exception:
        try:
            # Try parsing typical ISO 8601 variations
            date_clean = date_str.replace("Z", "+00:00")
            dt = datetime.fromisoformat(date_clean)
            return dt.isoformat()
        except Exception:
            # Fallback to current time if parsing fails completely
            return datetime.utcnow().isoformat() + "Z"

class ResilientScraper:
    def __init__(self, config_path: str = CONFIG_FILE_PATH):
        self.config_path = config_path
        self.sources = []
        self.semaphore = asyncio.Semaphore(3)  # Max 3 concurrent requests to avoid rate limits
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

    async def fetch_with_backoff(self, client: httpx.AsyncClient, name: str, url: str) -> Optional[str]:
        """Fetch URL content with exponential backoff and random jitter."""
        max_retries = 3
        base_delay = 2.0  # seconds

        for attempt in range(max_retries):
            # Rotate User-Agent per request to blend in
            headers = {
                "User-Agent": random.choice(USER_AGENTS),
                "Accept": "application/xhtml+xml,application/xml,text/html;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.5"
            }
            try:
                async with self.semaphore:
                    logger.info(f"Fetching {name} (Attempt {attempt + 1}/{max_retries})...")
                    response = await client.get(url, headers=headers, timeout=12.0, follow_redirects=True)
                
                # Check status code
                if response.status_code == 200:
                    return response.text
                elif response.status_code in (429, 503):
                    # Rate limit or temporary service issue: apply backoff
                    delay = (base_delay ** attempt) + random.uniform(0.5, 1.5)
                    logger.warning(f"Rate limited (Status {response.status_code}) on {name}. Backing off for {delay:.2f}s...")
                    await asyncio.sleep(delay)
                else:
                    logger.error(f"HTTP {response.status_code} error on {name}. Non-retryable.")
                    return None
            except (httpx.RequestError, asyncio.TimeoutError) as e:
                delay = (base_delay ** attempt) + random.uniform(0.5, 1.5)
                logger.warning(f"Request error on {name}: {e}. Retrying in {delay:.2f}s...")
                await asyncio.sleep(delay)
        
        logger.error(f"Failed to fetch {name} after {max_retries} attempts.")
        return None

    def parse_feed_content(self, xml_content: str, source_name: str) -> List[dict]:
        """Resiliently parse standard RSS or Atom XML feeds into structured dicts."""
        articles = []
        if not xml_content:
            return articles

        try:
            # Clean possible XML encoding mismatches
            # Parse the XML string
            root = ET.fromstring(xml_content.encode("utf-8", errors="ignore"))
            
            # Check if it is standard RSS
            channel = root.find("channel")
            if channel is not None:
                for item in channel.findall("item"):
                    title_elem = item.find("title")
                    link_elem = item.find("link")
                    desc_elem = item.find("description") or item.find("summary")
                    date_elem = item.find("pubDate") or item.find("date") or item.find("published")

                    title = clean_html(title_elem.text) if title_elem is not None else ""
                    url = link_elem.text.strip() if link_elem is not None else ""
                    summary = clean_html(desc_elem.text) if desc_elem is not None else None
                    published_raw = date_elem.text if date_elem is not None else ""

                    if title and url:
                        articles.append({
                            "title": title,
                            "summary": summary if summary else None,
                            "url": url,
                            "published": parse_rss_date(published_raw),
                            "source": source_name
                        })
            else:
                # Check if it is an Atom Feed (uses namespace)
                # Atom namespace is usually http://www.w3.org/2005/Atom
                ns = {"atom": "http://www.w3.org/2005/Atom"}
                entries = root.findall(".//atom:entry", ns) or root.findall(".//entry")
                for entry in entries:
                    # Try finding elements with or without namespace
                    title_elem = entry.find("atom:title", ns) or entry.find("title")
                    link_elem = entry.find("atom:link", ns) or entry.find("link")
                    desc_elem = entry.find("atom:summary", ns) or entry.find("summary") or entry.find("atom:content", ns) or entry.find("content")
                    date_elem = entry.find("atom:published", ns) or entry.find("published") or entry.find("atom:updated", ns) or entry.find("updated")

                    title = clean_html(title_elem.text) if title_elem is not None else ""
                    
                    url = ""
                    if link_elem is not None:
                        url = link_elem.get("href", "").strip() or link_elem.text or ""
                    
                    summary = clean_html(desc_elem.text) if desc_elem is not None else None
                    published_raw = date_elem.text if date_elem is not None else ""

                    if title and url:
                        articles.append({
                            "title": title,
                            "summary": summary if summary else None,
                            "url": url,
                            "published": parse_rss_date(published_raw),
                            "source": source_name
                        })
        except Exception as e:
            logger.error(f"XML Parsing failed for {source_name}: {e}")
            
            # Simple fallback regex-based feed parser in case XML structure is malformed
            logger.info(f"Attempting Regex Parsing fallback for {source_name}...")
            items = re.findall(r'<item>(.*?)</item>', xml_content, re.DOTALL)
            for item in items:
                title_match = re.search(r'<title>(.*?)</title>', item, re.DOTALL)
                link_match = re.search(r'<link>(.*?)</link>', item, re.DOTALL)
                desc_match = re.search(r'<description>(.*?)</description>', item, re.DOTALL)
                date_match = re.search(r'<pubDate>(.*?)</pubDate>', item, re.DOTALL)

                title = clean_html(title_match.group(1)) if title_match else ""
                url = link_match.group(1).strip() if link_match else ""
                summary = clean_html(desc_match.group(1)) if desc_match else None
                published_raw = date_match.group(1) if date_match else ""

                # Strip CDATA tags if present
                title = re.sub(r'<!\[CDATA\[(.*?)\]\]>', r'\1', title).strip()
                if url:
                    url = re.sub(r'<!\[CDATA\[(.*?)\]\]>', r'\1', url).strip()
                if summary:
                    summary = re.sub(r'<!\[CDATA\[(.*?)\]\]>', r'\1', summary).strip()

                if title and url:
                    articles.append({
                        "title": title,
                        "summary": summary if summary else None,
                        "url": url,
                        "published": parse_rss_date(published_raw),
                        "source": source_name
                    })

        return articles

    async def scrape_source(self, client: httpx.AsyncClient, source: dict) -> tuple:
        """Scrape a single approved source, parse it, validate records, and return results."""
        name = source["name"]
        url = source["url"]
        
        content = await self.fetch_with_backoff(client, name, url)
        if not content:
            # Fallback to direct HTML scraper link if feed fetching fails completely
            fallback_url = source.get("fallback_url")
            if fallback_url and fallback_url != url:
                logger.info(f"Feed failed. Attempting fallback URL for {name}: {fallback_url}")
                content = await self.fetch_with_backoff(client, name, fallback_url)
            
        if not content:
            return name, None, f"Source '{name}' fetch failed completely (all retries exhausted)"

        try:
            raw_articles = self.parse_feed_content(content, name)
            if not raw_articles:
                return name, None, f"Source '{name}' returned no articles or XML parsing was empty"

            valid_articles = []
            validation_errors = 0

            for raw in raw_articles:
                try:
                    # Enforce strict validation via Pydantic model
                    article = NewsArticle(**raw)
                    valid_articles.append(article)
                except ValidationError as ve:
                    validation_errors += 1
                    logger.debug(f"Pydantic Validation failed for an article in {name}: {ve}")
                    continue

            logger.info(f"Successfully processed {name}: {len(valid_articles)} validated articles ({validation_errors} malformed skipped).")
            if not valid_articles:
                return name, None, f"All articles from '{name}' failed validation checks"

            return name, SourceResult(source=name, articles=valid_articles), None

        except Exception as e:
            logger.error(f"Error scraping source {name}: {e}")
            return name, None, f"Unexpected error during parsing of '{name}': {str(e)}"

    async def run_pipeline(self) -> dict:
        """Run the full asynchronous pipeline to scrape all 10 sources and output schema-valid JSON."""
        logger.info("Starting autonomous financial news collection pipeline...")
        
        # Configure client with standard redirection/connect limits
        limits = httpx.Limits(max_keepalive_connections=5, max_connections=10)
        async with httpx.AsyncClient(limits=limits, verify=False) as client:
            tasks = [self.scrape_source(client, src) for src in self.sources]
            results = await asyncio.gather(*tasks)

        final_results = []
        errors_list = []

        for name, source_res, err in results:
            if err:
                logger.error(err)
                errors_list.append(err)
            elif source_res:
                final_results.append(source_res)

        # Build output structure using Pydantic model
        output_data = FinalOutput(results=final_results, errors=errors_list)
        
        # Write to JSON file
        output_file = OUTPUT_FILE_PATH
        try:
            with open(output_file, "w", encoding="utf-8") as f:
                f.write(output_data.model_dump_json(indent=2))
            logger.info(f"Successfully compiled all facts. Results saved to {output_file}.")
        except Exception as e:
            logger.critical(f"Failed to write final output to file: {e}")
            errors_list.append(f"Write failure: {str(e)}")

        return output_data.model_dump()

if __name__ == "__main__":
    # Check for programmatic boundaries (Google Antigravity SDK check)
    try:
        import google.antigravity
        logger.info("Google Antigravity SDK detected! Programmatic runtime active.")
    except ImportError:
        logger.info("Google Antigravity SDK not installed. Running under local resilient runtime framework.")

    # Execute async pipeline loop
    loop_output = asyncio.run(ResilientScraper().run_pipeline())
    print("\n--- PIPELINE EXECUTION OUTPUT ---\n")
    print(json.dumps(loop_output, indent=2))
