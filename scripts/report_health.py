"""Validate report schema, counts, uniqueness, and run freshness."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence


class ReportHealthError(RuntimeError):
    pass


def parse_timestamp(value: object) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def validate_report(
    path: Path,
    *,
    min_sources: int,
    min_articles: int,
    max_age_minutes: int,
) -> dict:
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReportHealthError(f"Could not read {path}: {exc}") from exc
    if not isinstance(report, dict):
        raise ReportHealthError("Report root must be an object")
    results = report.get("results")
    if not isinstance(results, list):
        raise ReportHealthError("Report must contain a results list")

    articles: list[dict] = []
    healthy_sources = 0
    malformed = 0
    timestamps: list[datetime] = []
    for group in results:
        if not isinstance(group, dict):
            malformed += 1
            continue
        group_articles = group.get("articles") or []
        if not isinstance(group_articles, list):
            malformed += 1
            continue
        if group.get("error") in (None, "") and (
            group_articles or int(group.get("fetched_entry_count") or 0) > 0
        ):
            healthy_sources += 1
        for article in group_articles:
            if not isinstance(article, dict):
                malformed += 1
                continue
            required = ("title", "url", "fetched_at", "source", "category")
            if any(not article.get(field) for field in required):
                malformed += 1
                continue
            timestamp = parse_timestamp(article.get("fetched_at"))
            if not timestamp:
                malformed += 1
                continue
            timestamps.append(timestamp)
            articles.append(article)

    urls = [str(article["url"]).split("#", 1)[0].strip() for article in articles]
    unique_urls = set(urls)
    newest = max(timestamps) if timestamps else None
    age_minutes = (
        (datetime.now(timezone.utc) - newest).total_seconds() / 60
        if newest
        else float("inf")
    )
    errors = []
    if healthy_sources < min_sources:
        errors.append(f"healthy sources {healthy_sources} < required {min_sources}")
    if len(articles) < min_articles:
        errors.append(f"valid articles {len(articles)} < required {min_articles}")
    if malformed:
        errors.append(f"malformed articles/groups: {malformed}")
    if len(unique_urls) != len(urls):
        errors.append(f"duplicate URLs: {len(urls) - len(unique_urls)}")
    if age_minutes > max_age_minutes:
        errors.append(
            f"newest fetched_at is {age_minutes:.1f} minutes old "
            f"(limit {max_age_minutes})"
        )

    summary = {
        "path": str(path),
        "healthy_sources": healthy_sources,
        "articles": len(articles),
        "unique_urls": len(unique_urls),
        "newest_fetched_at": newest.isoformat() if newest else None,
        "age_minutes": round(age_minutes, 1) if newest else None,
        "errors": errors,
    }
    if errors:
        raise ReportHealthError(json.dumps(summary, indent=2))
    return summary


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate a generated report")
    parser.add_argument("path", type=Path)
    parser.add_argument("--min-sources", type=int, required=True)
    parser.add_argument("--min-articles", type=int, required=True)
    parser.add_argument("--max-age-minutes", type=int, default=120)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        print(
            json.dumps(
                validate_report(
                    args.path,
                    min_sources=args.min_sources,
                    min_articles=args.min_articles,
                    max_age_minutes=args.max_age_minutes,
                ),
                indent=2,
            )
        )
        return 0
    except ReportHealthError as exc:
        print(f"REPORT HEALTH FAILURE: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
