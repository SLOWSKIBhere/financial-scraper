"""
daily_digest.py
---------------
Queries Notion DB for articles published/added in last 24 hours,
scores each by signal strength, returns top 5 as a WhatsApp-ready digest.

Signal scoring logic:
  +3  ticker mention ($AAPL, $BTC etc.)
  +2  high-impact words: crash, surge, rally, plunge, beat, miss, cut, hike, halt, ban
  +2  category = earnings
  +1  category = policy or crypto
  +1  has a summary (not title-only)
  -1  source = Unknown
"""

import os, re, requests
from datetime import datetime, timezone, timedelta

def get_notion_token() -> str:
    """Use the canonical token name while preserving older deployments."""
    return os.environ.get("NOTION_TOKEN") or os.environ.get("NOTION_ACCESS_TOKEN", "")


TOKEN    = get_notion_token()
DB_ID    = os.environ.get("NOTION_DB_ID", "38b2959f-6c14-819f-bff0-d31ea03e66ae")
HEADERS  = {
    "Authorization": f"Bearer {TOKEN}",
    "Notion-Version": "2022-06-28",
    "Content-Type": "application/json",
}

HIGH_IMPACT = re.compile(
    r"\b(crash|surge|rally|plunge|soar|beat|miss|cut|hike|halt|ban|default|"
    r"recession|bubble|spike|collapse|downgrade|upgrade|layoff|bankrupt)\b",
    re.IGNORECASE,
)
TICKER = re.compile(r"\$[A-Z]{1,5}\b")


def score_article(title: str, summary: str, category: str, source: str) -> int:
    text = f"{title} {summary}"
    score = 0
    if TICKER.search(text):
        score += 3
    score += len(HIGH_IMPACT.findall(text)) * 2
    score = min(score, 7)           # cap ticker + high-impact contribution
    if category == "earnings":
        score += 2
    elif category in ("policy", "crypto"):
        score += 1
    if summary and len(summary) > 30:
        score += 1
    if source.lower() in ("unknown", ""):
        score -= 1
    return min(max(score, 0), 10)


def fetch_recent_articles(hours: int = 24) -> list:
    """Pull all pages from Notion added in last `hours` hours."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    articles = []
    cursor = None

    while True:
        body = {
            "page_size": 100,
            "filter": {
                "timestamp": "created_time",
                "created_time": {"after": cutoff},
            },
            "sorts": [{"timestamp": "created_time", "direction": "descending"}],
        }
        if cursor:
            body["start_cursor"] = cursor

        r = requests.post(
            f"https://api.notion.com/v1/databases/{DB_ID}/query",
            headers=HEADERS,
            json=body,
            timeout=15,
        )
        if not r.ok:
            print(f"Notion query error {r.status_code}: {r.text[:120]}")
            break

        data = r.json()
        for page in data.get("results", []):
            props = page.get("properties", {})

            def text_prop(key):
                items = props.get(key, {}).get("rich_text", [])
                return items[0]["text"]["content"] if items else ""

            def title_prop():
                items = props.get("Title", {}).get("title", [])
                return items[0]["text"]["content"] if items else ""

            def select_prop(key):
                s = props.get(key, {}).get("select")
                return s["name"] if s else ""

            def url_prop():
                return props.get("URL", {}).get("url", "") or ""

            articles.append({
                "title":    title_prop(),
                "summary":  text_prop("Summary"),
                "url":      url_prop(),
                "source":   select_prop("Source"),
                "category": select_prop("Category"),
                "feed":     select_prop("Feed type"),
            })

        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")

    return articles


def build_digest() -> str:
    """Fetch, score, and format top 5 articles as WhatsApp-safe text."""
    articles = fetch_recent_articles(hours=24)
    if not articles:
        return "No new articles in the last 24 hours."

    # Score + sort
    scored = []
    for a in articles:
        s = score_article(a["title"], a["summary"], a["category"], a["source"])
        scored.append((s, a))
    scored.sort(key=lambda x: x[0], reverse=True)

    top5 = scored[:5]
    total = len(articles)

    today = datetime.now().strftime("%b %d")
    lines = [
        f"*📊 Daily Market Digest — {today}*",
        f"_{total} articles collected · Top 5 signals_",
        "",
    ]

    for i, (score, a) in enumerate(top5, 1):
        cat_emoji = {
            "earnings": "💰", "crypto": "🔗", "policy": "🏛️",
            "macro": "🌐", "markets": "📈",
        }.get(a["category"], "📰")

        lines.append(f"*{i}. {a['title'][:80]}*")
        lines.append(f"{cat_emoji} {a['category'].upper()} · {a['source']} · Score: {score}/10")
        if a["summary"]:
            lines.append(f"_{a['summary'][:120]}_")
        lines.append(a["url"])
        lines.append("")

    lines.append("_Powered by Financial Scraper + Notion_")
    return "\n".join(lines)


if __name__ == "__main__":
    print(build_digest())
