"""Deterministic local/CI orchestrator for collection, validation, and sync."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path
from typing import Sequence


ROOT = Path(__file__).resolve().parents[1]


def run(*arguments: str) -> None:
    command = [sys.executable, *arguments]
    print("+", " ".join(command), flush=True)
    subprocess.run(command, cwd=ROOT, check=True)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the financial scraper pipeline")
    parser.add_argument("--mode", choices=("rss", "community", "all"), default="all")
    parser.add_argument(
        "--sync",
        action="store_true",
        help="Write new articles to Notion after collection and validation",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    specs: list[tuple[str, str]] = []
    try:
        if args.mode in ("rss", "all"):
            run("collect.py")
            run(
                "scripts/report_health.py",
                "financial_report.json",
                "--min-sources",
                "3",
                "--min-articles",
                "25",
            )
            specs.append(("financial_report.json", "RSS"))
        if args.mode in ("community", "all"):
            run("community_feeds.py")
            run(
                "scripts/report_health.py",
                "community_report.json",
                "--min-sources",
                "2",
                "--min-articles",
                "5",
            )
            specs.append(("community_report.json", "Community"))
        if args.sync:
            command = ["scripts/notion_sync.py"]
            for path, feed_type in specs:
                command.extend(("--report", path, feed_type))
            run(*command)
        return 0
    except subprocess.CalledProcessError as exc:
        print(f"Pipeline stage failed with exit code {exc.returncode}", file=sys.stderr)
        return exc.returncode or 1


if __name__ == "__main__":
    raise SystemExit(main())
