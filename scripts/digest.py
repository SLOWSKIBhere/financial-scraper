"""Build and optionally deliver the daily market digest."""

from __future__ import annotations

import argparse
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Sequence

import requests

from notion_sync import NotionClient, NotionSyncError, NotionTarget, load_configuration


HIGH_IMPACT = re.compile(
    r"\b(crash|surge|rally|plunge|soar|beat|miss|cut|hike|halt|ban|default|"
    r"recession|bubble|spike|collapse|downgrade|upgrade|layoff|bankrupt)\b",
    re.IGNORECASE,
)
TICKER = re.compile(r"\$[A-Z]{1,5}\b")
OUTPUT_PATH = Path(__file__).resolve().parents[1] / "daily_digest.txt"


class DeliveryError(RuntimeError):
    pass


def score_article(title: str, summary: str, category: str, source: str) -> int:
    text = f"{title} {summary}"
    score = 3 if TICKER.search(text) else 0
    score += len(HIGH_IMPACT.findall(text)) * 2
    score = min(score, 7)
    if category == "earnings":
        score += 2
    elif category in ("policy", "crypto"):
        score += 1
    if summary and len(summary) > 30:
        score += 1
    if source.lower() in ("unknown", ""):
        score -= 1
    return min(max(score, 0), 10)


def rich_text(properties: dict, key: str, kind: str = "rich_text") -> str:
    items = properties.get(key, {}).get(kind, [])
    if not items:
        return ""
    return "".join(
        item.get("plain_text")
        or item.get("text", {}).get("content", "")
        for item in items
    )


def select_text(properties: dict, key: str) -> str:
    value = properties.get(key, {}).get("select")
    return value.get("name", "") if value else ""


def fetch_recent_articles(
    client: NotionClient,
    target: NotionTarget,
    hours: int = 24,
) -> list[dict]:
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    articles: list[dict] = []
    cursor = ""
    while True:
        payload: dict[str, object] = {
            "page_size": 100,
            "result_type": "page",
            "filter": {
                "timestamp": "created_time",
                "created_time": {"after": cutoff},
            },
            "sorts": [{"timestamp": "created_time", "direction": "descending"}],
        }
        if cursor:
            payload["start_cursor"] = cursor
        data = client.request(
            "POST", f"data_sources/{target.data_source_id}/query", payload
        )
        for page in data.get("results", []):
            properties = page.get("properties", {})
            articles.append(
                {
                    "title": rich_text(properties, "Title", "title"),
                    "summary": rich_text(properties, "Summary"),
                    "url": properties.get("URL", {}).get("url", "") or "",
                    "source": select_text(properties, "Source"),
                    "category": select_text(properties, "Category"),
                    "feed": select_text(properties, "Feed type"),
                }
            )
        if not data.get("has_more"):
            return articles
        cursor = data.get("next_cursor") or ""
        if not cursor:
            raise NotionSyncError(
                "Notion digest pagination returned has_more without next_cursor"
            )


def build_digest(articles: list[dict]) -> str:
    if not articles:
        return "No new articles in the last 24 hours."

    scored = [
        (
            score_article(
                article["title"],
                article["summary"],
                article["category"],
                article["source"],
            ),
            article,
        )
        for article in articles
    ]
    scored.sort(key=lambda pair: pair[0], reverse=True)
    today = datetime.now().strftime("%b %d")
    lines = [
        f"*📊 Daily Market Digest — {today}*",
        f"_{len(articles)} articles collected · Top 5 signals_",
        "",
    ]
    emojis = {
        "earnings": "💰",
        "crypto": "🔗",
        "policy": "🏛️",
        "macro": "🌐",
        "markets": "📈",
    }
    for index, (score, article) in enumerate(scored[:5], 1):
        category = article["category"] or "markets"
        lines.append(f"*{index}. {article['title'][:80]}*")
        lines.append(
            f"{emojis.get(category, '📰')} {category.upper()} · "
            f"{article['source']} · Score: {score}/10"
        )
        if article["summary"]:
            lines.append(f"_{article['summary'][:120]}_")
        lines.append(article["url"])
        lines.append("")
    lines.append("_Powered by Financial Scraper + Notion_")
    return "\n".join(lines)


def whatsapp_address(value: str) -> str:
    value = value.strip()
    return value if value.startswith("whatsapp:") else f"whatsapp:{value}"


def send_twilio(message: str) -> None:
    sid = os.getenv("TWILIO_ACCOUNT_SID", "").strip()
    token = os.getenv("TWILIO_AUTH_TOKEN", "").strip()
    sender = os.getenv("TWILIO_WHATSAPP_FROM", "").strip()
    recipient = os.getenv("TWILIO_WHATSAPP_TO", "").strip()
    if not all((sid, token, sender, recipient)):
        raise DeliveryError(
            "Twilio delivery requires TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, "
            "TWILIO_WHATSAPP_FROM, and TWILIO_WHATSAPP_TO"
        )
    response = requests.post(
        f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json",
        auth=(sid, token),
        data={
            "From": whatsapp_address(sender),
            "To": whatsapp_address(recipient),
            "Body": message,
        },
        timeout=20,
    )
    if not response.ok:
        raise DeliveryError(
            f"Twilio returned {response.status_code}: {response.text[:300]}"
        )


def send_webhook(message: str) -> None:
    url = os.getenv("DIGEST_WEBHOOK_URL", "").strip()
    if not url:
        raise DeliveryError("Webhook delivery requires DIGEST_WEBHOOK_URL")
    headers = {"Content-Type": "application/json"}
    bearer = os.getenv("DIGEST_WEBHOOK_TOKEN", "").strip()
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"
    response = requests.post(
        url,
        headers=headers,
        json={"text": message, "source": "financial-scraper"},
        timeout=20,
    )
    if not response.ok:
        raise DeliveryError(
            f"Digest webhook returned {response.status_code}: {response.text[:300]}"
        )


def configured_delivery() -> str:
    twilio_values = (
        os.getenv("TWILIO_ACCOUNT_SID"),
        os.getenv("TWILIO_AUTH_TOKEN"),
        os.getenv("TWILIO_WHATSAPP_FROM"),
        os.getenv("TWILIO_WHATSAPP_TO"),
    )
    if all(twilio_values):
        return "twilio"
    if os.getenv("DIGEST_WEBHOOK_URL"):
        return "webhook"
    return "none"


def deliver(message: str, provider: str) -> str:
    selected = configured_delivery() if provider == "auto" else provider
    if selected == "twilio":
        send_twilio(message)
    elif selected == "webhook":
        send_webhook(message)
    elif selected != "none":
        raise DeliveryError(f"Unknown delivery provider: {selected}")
    return selected


def atomic_write(path: Path, content: str) -> None:
    temp = path.with_suffix(path.suffix + ".tmp")
    try:
        temp.write_text(content, encoding="utf-8")
        temp.replace(path)
    finally:
        if temp.exists():
            temp.unlink()


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build and deliver the daily digest")
    parser.add_argument("--hours", type=int, default=24)
    parser.add_argument(
        "--provider",
        choices=("auto", "twilio", "webhook", "none"),
        default="auto",
    )
    parser.add_argument(
        "--require-delivery",
        action="store_true",
        help="Fail if no delivery provider is configured",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    repo_root = Path(__file__).resolve().parents[1]
    try:
        token, database_id, data_source_id = load_configuration(repo_root)
        client = NotionClient(token)
        target = client.resolve_target(database_id, data_source_id)
        message = build_digest(fetch_recent_articles(client, target, args.hours))
        atomic_write(OUTPUT_PATH, message + "\n")
        print(message)
        provider = deliver(message, args.provider)
        if provider == "none":
            if args.require_delivery:
                raise DeliveryError(
                    "No digest provider configured; add Twilio WhatsApp or webhook secrets"
                )
            print("Digest saved; no delivery provider is configured.")
        else:
            print(f"Digest delivered through {provider}.")
        return 0
    except (NotionSyncError, DeliveryError, requests.RequestException, OSError) as exc:
        print(f"FATAL: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
