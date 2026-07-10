"""
weight_nudge.py — Weekly keyword weight adjuster for financial-scraper.

Reads Firestore 'article_clicks' collection (last 7 days).
Counts clicks per category.
If a category gets 2x the average clicks → add 2 keyword slots to CATEGORY_RULES in collect_v2.py.
Writes the updated CATEGORY_RULES block back in-place.

No ML. No models. Pure dict counting + file write.

Auth: requires GOOGLE_APPLICATION_CREDENTIALS or Workload Identity Federation in CI.
Library: google-cloud-firestore (same version as Omniguide — not net-new).

Usage:
    python weight_nudge.py              # dry run (prints proposed changes)
    python weight_nudge.py --apply      # writes changes to collect_v2.py
"""

import os
import re
import json
import argparse
import logging
from datetime import datetime, timezone, timedelta
from collections import defaultdict

from google.cloud import firestore

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger("weight_nudge")

# Path to the scraper file containing CATEGORY_RULES
COLLECT_V2_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "collect_v2.py")

# GCP project — must match Omniguide's GCP_PROJECT_ID env var
GCP_PROJECT_ID = os.environ.get("GCP_PROJECT_ID", "omniguide-hackathon")

# Threshold: a category must have this multiple of average clicks to earn a nudge
NUDGE_THRESHOLD = 2.0

# How many keyword slots to add per nudged category
SLOTS_TO_ADD = 2

# Placeholder tokens added to CATEGORY_RULES to signal "add real keywords here"
# These are intentionally obvious so you know where to fill in real terms
PLACEHOLDER_TOKEN = "__nudge_slot__"


def fetch_click_counts() -> dict[str, int]:
    """
    Read Firestore 'article_clicks' from the last 7 days.
    Returns a dict: {category: click_count}.
    Joins on article_hash → category by reading financial_report.json locally.
    """
    db = firestore.Client(project=GCP_PROJECT_ID)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()

    logger.info("Fetching clicks from Firestore since %s", cutoff)

    # Pull all clicks in the last 7 days
    clicks_ref = db.collection("article_clicks")
    docs = clicks_ref.where("clicked_at", ">=", cutoff).stream()

    # Build hash → url map from local financial_report.json
    hash_to_category: dict[str, str] = {}
    report_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "financial_report.json")
    if os.path.exists(report_path):
        with open(report_path, "r", encoding="utf-8") as f:
            report = json.load(f)
        # financial_report.json structure: {results: [{source, articles: [{url, category, ...}]}]}
        for source_block in report.get("results", []):
            for article in source_block.get("articles", []):
                import hashlib
                url = article.get("url", "")
                h = hashlib.sha256(url.encode()).hexdigest()[:16]  # matches collect_v2 fingerprint
                hash_to_category[h] = article.get("category", "general")

    # Count clicks per category
    category_counts: dict[str, int] = defaultdict(int)
    total = 0
    for doc in docs:
        data = doc.to_dict()
        article_hash = data.get("article_hash", "")
        # Match hash prefix (collect_v2 uses first 16 chars of sha256)
        category = hash_to_category.get(article_hash[:16], "general")
        category_counts[category] += 1
        total += 1

    logger.info("Total clicks fetched: %d across %d categories", total, len(category_counts))
    return dict(category_counts)


def compute_nudges(counts: dict[str, int]) -> list[str]:
    """
    Determine which categories earned a nudge this week.
    A category qualifies if its click count >= NUDGE_THRESHOLD * average.
    Returns list of category names to nudge.
    """
    if not counts:
        logger.info("No click data — nothing to nudge")
        return []

    average = sum(counts.values()) / len(counts)
    nudge_floor = average * NUDGE_THRESHOLD

    nudged = [cat for cat, count in counts.items() if count >= nudge_floor]
    logger.info(
        "Average clicks: %.1f | Threshold: %.1f | Nudging: %s",
        average, nudge_floor, nudged
    )
    return nudged


def apply_nudges_to_file(categories: list[str], dry_run: bool = True) -> bool:
    """
    Add SLOTS_TO_ADD placeholder keyword entries to CATEGORY_RULES in collect_v2.py
    for each category in the nudge list.

    Operates via regex on the raw file — finds each category's keyword list
    and appends placeholder tokens. Prints a diff-style summary.

    Returns True if any changes were made (or would be made in dry_run).
    """
    if not os.path.exists(COLLECT_V2_PATH):
        logger.error("collect_v2.py not found at %s", COLLECT_V2_PATH)
        return False

    with open(COLLECT_V2_PATH, "r", encoding="utf-8") as f:
        source = f.read()

    modified = source
    changed = False

    for category in categories:
        # Match the category line in CATEGORY_RULES dict, e.g.:
        # "crypto":    ["bitcoin", "btc", ...],
        pattern = rf'("{category}":\s*\[)([^\]]*?)(\])'
        match = re.search(pattern, modified, re.DOTALL)

        if not match:
            logger.warning("Category '%s' not found in CATEGORY_RULES — skipping", category)
            continue

        existing_keywords = match.group(2).strip().rstrip(",")
        slots = ", ".join([f'"{PLACEHOLDER_TOKEN}_{category}_{i}"' for i in range(SLOTS_TO_ADD)])
        new_list = f"{existing_keywords}, {slots}" if existing_keywords else slots

        replacement = f"{match.group(1)}{new_list}{match.group(3)}"

        if dry_run:
            logger.info(
                "[DRY RUN] Would add %d slots to '%s': %s",
                SLOTS_TO_ADD, category, slots
            )
        else:
            modified = modified[:match.start()] + replacement + modified[match.end():]
            logger.info("Applied nudge to '%s': added %d placeholder slots", category, SLOTS_TO_ADD)
            changed = True

    if not dry_run and changed:
        with open(COLLECT_V2_PATH, "w", encoding="utf-8") as f:
            f.write(modified)
        logger.info("collect_v2.py updated in-place")

    return changed or (dry_run and bool(categories))


def main():
    parser = argparse.ArgumentParser(description="Weekly keyword weight nudger")
    parser.add_argument("--apply", action="store_true", help="Apply changes (default: dry run)")
    args = parser.parse_args()

    dry_run = not args.apply
    if dry_run:
        logger.info("DRY RUN mode — no files will be modified. Pass --apply to write changes.")

    counts = fetch_click_counts()
    nudge_categories = compute_nudges(counts)

    if not nudge_categories:
        logger.info("No categories qualify for a nudge this week. Done.")
        return

    changed = apply_nudges_to_file(nudge_categories, dry_run=dry_run)

    if dry_run:
        logger.info("Dry run complete. Run with --apply to write changes.")
    elif changed:
        logger.info("Nudge complete. Review placeholder tokens in collect_v2.py and replace with real keywords.")
    else:
        logger.info("No changes written.")


if __name__ == "__main__":
    main()
