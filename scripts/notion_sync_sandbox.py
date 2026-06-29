"""
notion_sync_sandbox.py
Sandbox-native sync — reads NOTION_ACCESS_TOKEN from environment (no .env needed).
Handles both RSS (financial_report.json) and community (community_report.json).
Deduplicates by URL before pushing.
"""

import json, os, re, time, requests
from datetime import datetime

TOKEN = os.environ.get('NOTION_ACCESS_TOKEN', '')
DB_ID = "38b2959f-6c14-819f-bff0-d31ea03e66ae"
HEADERS = {
    "Authorization": f"Bearer {TOKEN}",
    "Notion-Version": "2022-06-28",
    "Content-Type": "application/json"
}
RATE_DELAY = 0.35  # Notion allows ~3 req/sec

CATEGORY_MAP = {
    "crypto":"crypto","bitcoin":"crypto","defi":"crypto","cryptocurrency":"crypto",
    "macro":"macro","economy":"macro","fed":"policy","rates":"policy",
    "earnings":"earnings","ipo":"markets","commodities":"markets",
    "markets":"markets","policy":"policy","general":"markets","technology":"markets",
}

def clean_html(text):
    return re.sub(r'<[^>]+>', '', text or '').strip()

def normalize_category(raw):
    return CATEGORY_MAP.get((raw or '').lower().strip(), 'markets')

def safe_date(pub):
    for fmt in ("%Y-%m-%dT%H:%M:%S+00:00", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(pub[:len(fmt)-2], fmt[:len(pub)])
        except: continue
    return None

def get_existing_urls():
    urls = set()
    cursor = None
    while True:
        body = {"page_size": 100}
        if cursor: body["start_cursor"] = cursor
        r = requests.post(f"https://api.notion.com/v1/databases/{DB_ID}/query",
                          headers=HEADERS, json=body)
        if not r.ok:
            print(f"  Query error: {r.status_code}")
            break
        data = r.json()
        for page in data.get('results', []):
            u = page.get('properties',{}).get('URL',{}).get('url','')
            if u: urls.add(u)
        if not data.get('has_more'): break
        cursor = data.get('next_cursor')
    return urls

def push_article(a, feed_type, existing_urls):
    url = (a.get('url') or a.get('link') or '').strip()
    title = (a.get('title') or '').strip()[:200]
    if not url or not title or url in existing_urls:
        return 'skip'

    summary = clean_html(a.get('summary',''))[:400]
    source = (a.get('source') or 'Unknown')[:100]
    category = normalize_category(a.get('category',''))
    pub = a.get('published','')

    props = {
        "Title": {"title": [{"text": {"content": title}}]},
        "URL": {"url": url},
        "Summary": {"rich_text": [{"text": {"content": summary}}]},
        "Source": {"select": {"name": source}},
        "Category": {"select": {"name": category}},
        "Feed type": {"select": {"name": feed_type}},
    }
    if pub:
        dt = safe_date(pub)
        if dt:
            props["Published date"] = {"date": {"start": dt.strftime("%Y-%m-%d")}}

    r = requests.post("https://api.notion.com/v1/pages",
                      headers=HEADERS, json={"parent": {"database_id": DB_ID}, "properties": props})
    if r.ok:
        existing_urls.add(url)
        return 'pushed'
    else:
        print(f"  Push error {r.status_code}: {r.text[:80]}")
        return 'error'

def flatten(report, feed_type):
    articles = []
    if not report: return articles
    results = report.get('results', [])
    for block in results:
        if isinstance(block, dict):
            src = block.get('source', '')
            for a in block.get('articles', []):
                if not a.get('source'): a['source'] = src
                articles.append((a, feed_type))
    return articles

def sync(report_path, feed_type, existing_urls):
    if not os.path.exists(report_path):
        print(f"  No file: {report_path}")
        return 0, 0, 0
    with open(report_path) as f:
        report = json.load(f)
    items = flatten(report, feed_type)
    pushed = skipped = errors = 0
    for a, ft in items:
        result = push_article(a, ft, existing_urls)
        if result == 'pushed': pushed += 1
        elif result == 'skip': skipped += 1
        else: errors += 1
        if result == 'pushed': time.sleep(RATE_DELAY)
    return pushed, skipped, errors

if __name__ == '__main__':
    print("Fetching existing Notion URLs...")
    existing = get_existing_urls()
    print(f"Existing: {len(existing)}")

    r1p, r1s, r1e = sync('/tmp/fscraper/financial_report.json', 'RSS', existing)
    print(f"RSS: {r1p} pushed | {r1s} skipped | {r1e} errors")

    r2p, r2s, r2e = sync('/tmp/fscraper/community_report.json', 'Community', existing)
    print(f"Community: {r2p} pushed | {r2s} skipped | {r2e} errors")

    total = r1p + r2p
    print(f"\nTotal pushed: {total} | Total in Notion: ~{len(existing)}")
