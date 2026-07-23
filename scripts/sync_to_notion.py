"""
sync_to_notion.py
Reads collect_report.json and community_report.json from the financial scraper
and syncs articles to a Notion database as visual cards.
"""

import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

try:
    import requests
except ImportError:
    print("ERROR: requests not installed. Run: pip install requests python-dotenv")
    sys.exit(1)

try:
    from dotenv import load_dotenv
except ImportError:
    print("ERROR: python-dotenv not installed. Run: pip install requests python-dotenv")
    sys.exit(1)


# ── env ──────────────────────────────────────────────────────────────────────

REQUEST_TIMEOUT_SECONDS = 15


def get_notion_token():
    """Use the canonical token name while preserving older deployments."""
    return os.getenv("NOTION_TOKEN") or os.getenv("NOTION_ACCESS_TOKEN")


def load_env():
    env_path = Path(__file__).parent.parent / ".env"
    if env_path.exists():
        load_dotenv(env_path)

    token = get_notion_token()
    db_id = os.getenv("NOTION_DB_ID")

    if not token or not db_id:
        print("ERROR: Notion credentials are missing.")
        print("\nSet environment variables or add them to .env:")
        print("  NOTION_TOKEN=secret_...")
        print("  NOTION_DB_ID=38b2959f-6c14-819f-bff0-d31ea03e66ae")
        print("\nNOTION_ACCESS_TOKEN remains supported as a legacy token name.")
        sys.exit(1)

    return token, db_id


# ── notion api ────────────────────────────────────────────────────────────────

def notion_request(method, endpoint, token, payload=None, retries=3):
    url = f"https://api.notion.com/v1/{endpoint}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Notion-Version": "2022-06-28",
        "Content-Type": "application/json",
    }
    for attempt in range(retries):
        resp = requests.request(
            method,
            url,
            headers=headers,
            json=payload,
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        if resp.status_code == 429:
            wait = int(resp.headers.get("Retry-After", 2))
            print(f"  Rate limited — waiting {wait}s...")
            time.sleep(wait)
            continue
        if not resp.ok:
            print(f"  Notion API error {resp.status_code}: {resp.text[:200]}")
            resp.raise_for_status()
        return resp.json()
    raise Exception("Max retries hit on Notion API")


def validate_db(token, db_id):
    data = notion_request("GET", f"databases/{db_id}", token)
    props = data.get("properties", {})
    required = ["Title", "Source", "Category", "Summary", "URL", "Published date", "Feed type"]
    missing = [p for p in required if p not in props]
    if missing:
        print(f"ERROR: Notion DB missing properties: {missing}")
        sys.exit(1)
    print(f"  DB validated — {len(props)} properties found ✅")


def url_exists(url, token, db_id):
    payload = {
        "filter": {
            "property": "URL",
            "url": {"equals": url}
        },
        "page_size": 1
    }
    data = notion_request("POST", f"databases/{db_id}/query", token, payload)
    return len(data.get("results", [])) > 0


def create_page(article, token, db_id):
    title = (article.get("title") or "Untitled")[:200]
    source = (article.get("source") or "Unknown")[:100]
    category = normalize_category(article)
    summary = (article.get("summary") or "")[:1800]
    url = article.get("url") or article.get("link") or ""
    feed_type = article.get("feed_type", "RSS")

    # Published date
    pub = article.get("published") or article.get("fetched_at") or ""
    date_val = None
    if pub:
        try:
            # Handle various formats
            for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
                try:
                    dt = datetime.strptime(pub[:19], fmt[:len(pub[:19])])
                    date_val = dt.strftime("%Y-%m-%d")
                    break
                except:
                    continue
        except:
            pass

    props = {
        "Title": {"title": [{"text": {"content": title}}]},
        "Source": {"select": {"name": source}},
        "Category": {"select": {"name": category}},
        "Summary": {"rich_text": [{"text": {"content": summary}}]},
        "Feed type": {"select": {"name": feed_type}},
    }

    if url:
        props["URL"] = {"url": url}
    if date_val:
        props["Published date"] = {"date": {"start": date_val}}

    payload = {
        "parent": {"database_id": db_id},
        "properties": props
    }
    notion_request("POST", "pages", token, payload)


# ── data helpers ──────────────────────────────────────────────────────────────

CATEGORY_MAP = {
    "crypto": "crypto",
    "macro": "macro",
    "earnings": "earnings",
    "policy": "policy",
    "markets": "markets",
    "ipo": "markets",
    "commodities": "markets",
    "general": "markets",
    "technology": "markets",
    "economy": "macro",
    "fed": "policy",
    "rates": "policy",
}

def normalize_category(article):
    raw = (article.get("category") or "").lower().strip()
    return CATEGORY_MAP.get(raw, "markets")


def load_report(path):
    p = Path(path)
    if not p.exists():
        return None
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)


def flatten_articles(report, feed_type):
    articles = []
    if not report:
        return articles

    # Shape: {results: [{source, articles: [...]}]}
    results = report.get("results") or report.get("sources") or []
    if results and isinstance(results, list):
        for group in results:
            group_source = group.get("source", "")
            for art in group.get("articles", []):
                art["feed_type"] = feed_type
                if not art.get("source"):
                    art["source"] = group_source
                articles.append(art)
    # Shape: flat list
    elif isinstance(report, list):
        for art in report:
            art["feed_type"] = feed_type
            articles.append(art)

    return articles


# ── main ──────────────────────────────────────────────────────────────────────

def main():
    print("sync_to_notion.py — Financial Scraper → Notion")
    print("=" * 48)

    token, db_id = load_env()
    print(f"  DB ID: {db_id[:8]}...")

    validate_db(token, db_id)

    # Resolve paths relative to this script's location (scripts/)
    base = Path(__file__).parent.parent

    rss_path = base / "collect_report.json"
    if not rss_path.exists():
        rss_path = base / "financial_report.json"

    community_path = base / "community_report.json"

    rss_report = load_report(rss_path)
    community_report = load_report(community_path)

    if not rss_report and not community_report:
        print("ERROR: No report files found. Run the scraper first.")
        print(f"  Looking for: {rss_path}")
        print(f"  Looking for: {community_path}")
        sys.exit(1)

    articles = []
    if rss_report:
        rss_articles = flatten_articles(rss_report, "RSS")
        print(f"  RSS report: {len(rss_articles)} articles")
        articles.extend(rss_articles)
    if community_report:
        comm_articles = flatten_articles(community_report, "Community")
        print(f"  Community report: {len(comm_articles)} articles")
        articles.extend(comm_articles)

    print(f"\n  Total articles to process: {len(articles)}")
    print()

    synced = 0
    skipped = 0
    errors = 0

    for i, article in enumerate(articles):
        url = article.get("url") or article.get("link") or ""
        title = (article.get("title") or "Untitled")[:60]

        if not url:
            skipped += 1
            continue

        try:
            if url_exists(url, token, db_id):
                skipped += 1
                if (i + 1) % 20 == 0:
                    print(f"  [{i+1}/{len(articles)}] {skipped} duplicates skipped so far...")
                continue

            create_page(article, token, db_id)
            synced += 1

            if synced % 10 == 0:
                print(f"  ✅ {synced} synced so far...")

            # Small delay to be nice to Notion API
            time.sleep(0.35)

        except Exception as e:
            errors += 1
            print(f"  ⚠️  Error on '{title}': {e}")
            continue

    print()
    print("=" * 48)
    print(f"✅ {synced} articles synced")
    print(f"⏭️  {skipped} skipped (duplicates or no URL)")
    if errors:
        print(f"⚠️  {errors} errors")
    print("Done.")


if __name__ == "__main__":
    main()
