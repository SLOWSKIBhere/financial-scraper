# Financial Scraper

Automated financial news pipeline collecting and syncing 400+ articles/day to Notion.

## What it does
- RSS pipeline: 303 articles/day from 10 sources (WSJ, Bloomberg, CNBC, Reuters, etc.)
- Community feeds: 140 articles/day from 5 sources (Reddit, SeekingAlpha, etc.)
- Auto-syncs to Notion with deduplication
- Daily signal-scored digest sent to WhatsApp at 8AM ET

## Structure
```
collect.py           — RSS scraper (10 sources)
community_feeds.py   — Community feed scraper (5 sources)
scripts/
  sync_to_notion.py        — Local Notion sync
  notion_sync_sandbox.py   — Sandbox-native sync
  daily_digest.py          — Signal scoring + WhatsApp digest
config.json          — Source configuration
```

## Categories
crypto · macro · earnings · policy · markets · commodities

## Automations
- 12:05 PM ET — Community feeds run + Notion sync
- 4:00 PM ET  — RSS scraper run + Notion sync
- 8:00 AM ET  — Daily digest sent to WhatsApp

## Stack
Python 3.13 · Notion API · GitHub Actions (via agent automations)
