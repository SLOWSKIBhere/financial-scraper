"""Reliable Notion sync for financial scraper reports.

This module uses Notion's current data-source API, discovers the single data
source in the configured database when necessary, deduplicates in one paginated
read, and returns a non-zero status for every configuration or write failure.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable, Sequence

import requests
from dotenv import load_dotenv


API_ROOT = "https://api.notion.com/v1"
NOTION_VERSION = os.getenv("NOTION_VERSION", "2026-03-11")
REQUEST_TIMEOUT_SECONDS = 20
DEFAULT_NOTION_DB_ID = "38b2959f-6c14-819f-bff0-d31ea03e66ae"
TOKEN_ENV_NAMES = ("NOTION_TOKEN", "NOTION_ACCESS_TOKEN", "NOTION_API_KEY")
REQUIRED_PROPERTIES = {
    "Title",
    "Source",
    "Category",
    "Summary",
    "URL",
    "Published date",
    "Feed type",
}
CATEGORY_MAP = {
    "crypto": "crypto",
    "bitcoin": "crypto",
    "defi": "crypto",
    "cryptocurrency": "crypto",
    "macro": "macro",
    "economy": "macro",
    "fed": "policy",
    "rates": "policy",
    "earnings": "earnings",
    "ipo": "markets",
    "commodities": "markets",
    "markets": "markets",
    "policy": "policy",
    "general": "markets",
    "technology": "markets",
}


class NotionSyncError(RuntimeError):
    """Raised when configuration or a Notion request is not recoverable."""


@dataclass(frozen=True)
class NotionTarget:
    database_id: str
    data_source_id: str
    properties: dict


def get_notion_token() -> str:
    """Return the first configured canonical or legacy Notion token."""
    for name in TOKEN_ENV_NAMES:
        value = os.getenv(name, "").strip()
        if value:
            return value
    return ""


def load_configuration(repo_root: Path) -> tuple[str, str, str]:
    """Load local .env values, then validate runtime configuration."""
    env_path = repo_root / ".env"
    if env_path.exists():
        load_dotenv(env_path)

    token = get_notion_token()
    database_id = os.getenv("NOTION_DB_ID", DEFAULT_NOTION_DB_ID).strip()
    data_source_id = os.getenv("NOTION_DATA_SOURCE_ID", "").strip()
    if not token:
        aliases = ", ".join(TOKEN_ENV_NAMES)
        raise NotionSyncError(f"Missing Notion token; set one of: {aliases}")
    if not database_id and not data_source_id:
        raise NotionSyncError(
            "Set NOTION_DB_ID or NOTION_DATA_SOURCE_ID for the target database"
        )
    return token, database_id, data_source_id


class NotionClient:
    def __init__(
        self,
        token: str,
        *,
        session: requests.Session | None = None,
        sleep=time.sleep,
        retries: int = 4,
    ) -> None:
        self.session = session or requests.Session()
        self.sleep = sleep
        self.retries = retries
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Notion-Version": NOTION_VERSION,
            "Content-Type": "application/json",
        }

    def request(self, method: str, path: str, payload: dict | None = None) -> dict:
        """Make a bounded, retrying request without ever logging credentials."""
        url = f"{API_ROOT}/{path.lstrip('/')}"
        for attempt in range(self.retries):
            try:
                response = self.session.request(
                    method,
                    url,
                    headers=self.headers,
                    json=payload,
                    timeout=REQUEST_TIMEOUT_SECONDS,
                )
            except requests.RequestException as exc:
                if attempt == self.retries - 1:
                    raise NotionSyncError(
                        f"Notion network request failed after {self.retries} attempts"
                    ) from exc
                self.sleep(2 ** attempt)
                continue

            if response.status_code == 429 or 500 <= response.status_code < 600:
                if attempt < self.retries - 1:
                    retry_after = response.headers.get("Retry-After")
                    try:
                        delay = max(float(retry_after), 0.0) if retry_after else 2 ** attempt
                    except ValueError:
                        delay = 2 ** attempt
                    self.sleep(delay)
                    continue

            if not response.ok:
                body = re.sub(r"\s+", " ", response.text or "")[:300]
                raise NotionSyncError(
                    f"Notion API {response.status_code} for {method} {path}: {body}"
                )

            try:
                return response.json()
            except ValueError as exc:
                raise NotionSyncError(
                    f"Notion returned invalid JSON for {method} {path}"
                ) from exc

        raise NotionSyncError(f"Notion request exhausted retries: {method} {path}")

    def resolve_target(
        self,
        database_id: str,
        data_source_id: str = "",
    ) -> NotionTarget:
        """Resolve and validate a data source for current Notion APIs."""
        resolved_database_id = database_id
        resolved_data_source_id = data_source_id

        if not resolved_data_source_id:
            database = self.request("GET", f"databases/{database_id}")
            sources = database.get("data_sources") or []
            if len(sources) != 1:
                raise NotionSyncError(
                    "The configured database must contain exactly one data source, "
                    "or NOTION_DATA_SOURCE_ID must select one explicitly"
                )
            resolved_data_source_id = sources[0].get("id", "")
            if not resolved_data_source_id:
                raise NotionSyncError("Notion database response omitted its data source ID")

        data_source = self.request(
            "GET", f"data_sources/{resolved_data_source_id}"
        )
        if not resolved_database_id:
            resolved_database_id = (
                data_source.get("parent", {}).get("database_id", "")
            )
        properties = data_source.get("properties") or {}
        missing = sorted(REQUIRED_PROPERTIES - set(properties))
        if missing:
            raise NotionSyncError(
                "Notion data source is missing required properties: "
                + ", ".join(missing)
            )
        return NotionTarget(
            database_id=resolved_database_id,
            data_source_id=resolved_data_source_id,
            properties=properties,
        )

    def existing_urls(self, target: NotionTarget) -> set[str]:
        """Read every existing URL once using paginated data-source queries."""
        urls: set[str] = set()
        cursor = ""
        while True:
            payload: dict[str, object] = {"page_size": 100, "result_type": "page"}
            if cursor:
                payload["start_cursor"] = cursor
            data = self.request(
                "POST",
                f"data_sources/{target.data_source_id}/query",
                payload,
            )
            for page in data.get("results", []):
                value = page.get("properties", {}).get("URL", {}).get("url")
                if value:
                    urls.add(value.strip())
            if not data.get("has_more"):
                return urls
            cursor = data.get("next_cursor") or ""
            if not cursor:
                raise NotionSyncError(
                    "Notion pagination returned has_more without next_cursor"
                )

    def create_page(self, target: NotionTarget, article: dict) -> None:
        title = clean_text(article.get("title") or "Untitled")[:200]
        source = clean_text(article.get("source") or "Unknown")[:100]
        category = normalize_category(article.get("category"))
        summary = clean_text(article.get("summary"))[:1800]
        url = article_url(article)
        feed_type = clean_text(article.get("feed_type") or "RSS")[:100]
        published = notion_date(
            article.get("published") or article.get("fetched_at")
        )

        properties = {
            "Title": {"title": [{"text": {"content": title}}]},
            "Source": {"select": {"name": source}},
            "Category": {"select": {"name": category}},
            "Summary": {"rich_text": [{"text": {"content": summary}}]},
            "URL": {"url": url},
            "Feed type": {"select": {"name": feed_type}},
        }
        if published:
            properties["Published date"] = {"date": {"start": published}}

        self.request(
            "POST",
            "pages",
            {
                "parent": {
                    "type": "data_source_id",
                    "data_source_id": target.data_source_id,
                },
                "properties": properties,
            },
        )


def clean_text(value: object) -> str:
    text = re.sub(r"<[^>]+>", " ", str(value or ""))
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def article_url(article: dict) -> str:
    return clean_text(article.get("url") or article.get("link"))


def normalize_category(value: object) -> str:
    return CATEGORY_MAP.get(clean_text(value).lower(), "markets")


def notion_date(value: object) -> str | None:
    raw = clean_text(value)
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        prefix = raw[:10]
        return prefix if re.fullmatch(r"\d{4}-\d{2}-\d{2}", prefix) else None


def load_report(path: Path) -> object:
    if not path.exists():
        raise NotionSyncError(f"Report not found: {path}")
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise NotionSyncError(f"Could not read report {path}: {exc}") from exc


def flatten_articles(report: object, feed_type: str) -> list[dict]:
    """Normalize supported flat and grouped report formats without mutation."""
    if isinstance(report, list):
        groups: Iterable[dict] = ({"articles": report},)
    elif isinstance(report, dict):
        groups = report.get("results") or report.get("sources") or ()
    else:
        raise NotionSyncError("Report root must be an object or an article list")

    articles: list[dict] = []
    for group in groups:
        if not isinstance(group, dict):
            continue
        group_source = group.get("source", "")
        for raw in group.get("articles") or ():
            if not isinstance(raw, dict):
                continue
            article = dict(raw)
            article["feed_type"] = feed_type
            article.setdefault("source", group_source)
            articles.append(article)
    return articles


def collect_unique_articles(
    report_specs: Sequence[tuple[Path, str]],
) -> tuple[dict[str, dict], int, int]:
    unique: dict[str, dict] = {}
    total = 0
    missing_url = 0
    for path, feed_type in report_specs:
        articles = flatten_articles(load_report(path), feed_type)
        print(f"  {feed_type}: {len(articles)} articles from {path}")
        for article in articles:
            total += 1
            url = article_url(article)
            if not url:
                missing_url += 1
                continue
            unique.setdefault(url, article)
    return unique, total, missing_url


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Sync scraper reports to Notion")
    parser.add_argument(
        "--report",
        action="append",
        nargs=2,
        metavar=("PATH", "FEED_TYPE"),
        help="Report path and Notion Feed type; may be repeated",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate and count report data without calling Notion",
    )
    parser.add_argument(
        "--max-errors",
        type=int,
        default=int(os.getenv("NOTION_MAX_SYNC_ERRORS", "5")),
        help="Abort after this many page-create errors (default: 5)",
    )
    return parser.parse_args(argv)


def default_report_specs(repo_root: Path) -> list[tuple[Path, str]]:
    return [
        (repo_root / "financial_report.json", "RSS"),
        (repo_root / "community_report.json", "Community"),
    ]


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    repo_root = Path(__file__).resolve().parents[1]
    specs = (
        [(Path(path), feed_type) for path, feed_type in args.report]
        if args.report
        else default_report_specs(repo_root)
    )

    print("Financial Scraper -> Notion")
    print("=" * 48)
    try:
        unique, total, missing_url = collect_unique_articles(specs)
        print(f"  Input articles: {total}")
        print(f"  Unique URLs: {len(unique)}")
        print(f"  Missing URLs: {missing_url}")

        if args.dry_run:
            print("Dry run complete; no Notion requests were made.")
            return 0

        token, database_id, data_source_id = load_configuration(repo_root)
        client = NotionClient(token)
        target = client.resolve_target(database_id, data_source_id)
        print(f"  Data source: {target.data_source_id[:8]}...")
        existing = client.existing_urls(target)
        candidates = [
            article for url, article in unique.items() if url not in existing
        ]
        print(f"  Existing URLs: {len(existing)}")
        print(f"  New articles: {len(candidates)}")

        synced = 0
        errors: list[str] = []
        max_errors = max(args.max_errors, 1)
        for article in candidates:
            try:
                client.create_page(target, article)
                synced += 1
                if synced % 10 == 0:
                    print(f"  Synced {synced}/{len(candidates)}")
            except NotionSyncError as exc:
                title = clean_text(article.get("title"))[:80]
                errors.append(f"{title}: {exc}")
                print(f"  ERROR {errors[-1]}", file=sys.stderr)
                if len(errors) >= max_errors:
                    break

        summary = {
            "input_articles": total,
            "unique_urls": len(unique),
            "existing_urls": len(existing),
            "new_articles": len(candidates),
            "synced": synced,
            "skipped": len(unique) - len(candidates) + missing_url,
            "errors": len(errors),
        }
        print(json.dumps(summary, indent=2))
        if errors:
            raise NotionSyncError(
                f"Sync failed with {len(errors)} page-create error(s)"
            )
        return 0
    except (NotionSyncError, OSError, ValueError) as exc:
        print(f"FATAL: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
