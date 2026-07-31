import asyncio
import json
import logging
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

with patch.object(logging, "FileHandler", lambda *args, **kwargs: logging.NullHandler()):
    import collect
    import community_feeds
import digest
import notion_sync
import report_health


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text="", headers=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = text
        self.headers = headers or {}
        self.ok = 200 <= status_code < 300

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if not self.responses:
            raise AssertionError("No fake response remains")
        return self.responses.pop(0)


def required_properties():
    return {name: {} for name in notion_sync.REQUIRED_PROPERTIES}


def writable_temporary_directory():
    """Use Codex's Windows writable temp root; default normally elsewhere."""
    preferred = os.getenv("TEST_TMPDIR")
    if not preferred and os.name == "nt" and Path("C:/tmp").exists():
        preferred = "C:/tmp"
    return tempfile.TemporaryDirectory(dir=preferred)


class NotionReliabilityTests(unittest.TestCase):
    def test_all_historical_token_names_are_supported(self):
        for name in notion_sync.TOKEN_ENV_NAMES:
            with self.subTest(name=name), patch.dict(
                os.environ, {name: "token"}, clear=True
            ):
                self.assertEqual(notion_sync.get_notion_token(), "token")

    def test_current_data_source_api_is_discovered_and_used(self):
        session = FakeSession(
            [
                FakeResponse(payload={"data_sources": [{"id": "source-id"}]}),
                FakeResponse(payload={"properties": required_properties()}),
                FakeResponse(payload={}),
            ]
        )
        client = notion_sync.NotionClient("secret", session=session, sleep=lambda _: None)
        target = client.resolve_target("database-id")
        client.create_page(
            target,
            {
                "title": "Headline",
                "summary": "Summary",
                "url": "https://example.test/article",
                "source": "Reuters",
                "category": "macro",
                "feed_type": "RSS",
                "published": "2026-07-30T12:00:00Z",
            },
        )

        self.assertEqual(target.data_source_id, "source-id")
        self.assertTrue(session.calls[0][1].endswith("/databases/database-id"))
        self.assertTrue(session.calls[1][1].endswith("/data_sources/source-id"))
        create_payload = session.calls[2][2]["json"]
        self.assertEqual(
            create_payload["parent"],
            {"type": "data_source_id", "data_source_id": "source-id"},
        )
        self.assertEqual(
            session.calls[0][2]["headers"]["Notion-Version"], "2026-03-11"
        )

    def test_transient_server_error_retries(self):
        sleeps = []
        session = FakeSession(
            [
                FakeResponse(503, text="temporary"),
                FakeResponse(200, payload={"ok": True}),
            ]
        )
        client = notion_sync.NotionClient(
            "secret", session=session, sleep=sleeps.append
        )
        self.assertEqual(client.request("GET", "databases/example"), {"ok": True})
        self.assertEqual(len(session.calls), 2)
        self.assertEqual(sleeps, [1])

    def test_auth_failure_is_not_converted_to_success(self):
        session = FakeSession([FakeResponse(401, text="unauthorized")])
        client = notion_sync.NotionClient("bad", session=session, sleep=lambda _: None)
        with self.assertRaises(notion_sync.NotionSyncError):
            client.request("GET", "databases/example")

    def test_existing_urls_are_paginated_once(self):
        session = FakeSession(
            [
                FakeResponse(
                    payload={
                        "results": [
                            {"properties": {"URL": {"url": "https://one.test"}}}
                        ],
                        "has_more": True,
                        "next_cursor": "next",
                    }
                ),
                FakeResponse(
                    payload={
                        "results": [
                            {"properties": {"URL": {"url": "https://two.test"}}}
                        ],
                        "has_more": False,
                    }
                ),
            ]
        )
        client = notion_sync.NotionClient("secret", session=session, sleep=lambda _: None)
        target = notion_sync.NotionTarget("db", "source", required_properties())
        self.assertEqual(
            client.existing_urls(target),
            {"https://one.test", "https://two.test"},
        )
        self.assertEqual(session.calls[1][2]["json"]["start_cursor"], "next")

    def test_iso_dates_are_normalized(self):
        for raw in (
            "2026-07-30T12:00:00+00:00",
            "2026-07-30T12:00:00Z",
            "2026-07-30",
        ):
            with self.subTest(raw=raw):
                self.assertEqual(notion_sync.notion_date(raw), "2026-07-30")


class CollectorFailureTests(unittest.TestCase):
    def test_total_rss_failure_preserves_previous_report_and_fails_health(self):
        with writable_temporary_directory() as directory:
            temp = Path(directory)
            config_path = temp / "config.json"
            output_path = temp / "financial_report.json"
            metrics_path = temp / "metrics.json"
            config_path.write_text(
                json.dumps(
                    {
                        "approved_sources": [
                            {"name": "Broken", "url": "https://example.test/feed"}
                        ]
                    }
                ),
                encoding="utf-8",
            )
            output_path.write_text("previous-good-report", encoding="utf-8")
            scraper = collect.ResilientScraper(str(config_path))

            async def fail_source(_client, source):
                metrics = scraper._empty_metrics(source["name"])
                metrics["errors"].append("offline")
                return source["name"], None, "offline", metrics

            scraper.scrape_source = fail_source
            with patch.object(collect, "OUTPUT_FILE_PATH", str(output_path)), patch.object(
                collect, "METRICS_FILE_PATH", str(metrics_path)
            ):
                asyncio.run(scraper.run_pipeline())

            self.assertFalse(scraper.last_run_healthy)
            self.assertEqual(
                output_path.read_text(encoding="utf-8"), "previous-good-report"
            )
            self.assertTrue(metrics_path.exists())

    def test_total_community_failure_fails_health(self):
        scraper = community_feeds.CommunityFeedsScraper()

        def fail_source(_client, name, _url, _category):
            return community_feeds.SourceResult(source=name, error="offline")

        scraper.fetch_source = fail_source
        with patch.object(community_feeds.time, "sleep", lambda _: None):
            output = scraper.run()
        self.assertFalse(scraper.last_run_healthy)
        self.assertEqual(output.total_articles, 0)


class ReportHealthTests(unittest.TestCase):
    def make_report(self, fetched_at, duplicate=False):
        articles = [
            {
                "title": "One",
                "url": "https://example.test/one",
                "fetched_at": fetched_at,
                "source": "Example",
                "category": "markets",
            },
            {
                "title": "Two",
                "url": "https://example.test/one" if duplicate else "https://example.test/two",
                "fetched_at": fetched_at,
                "source": "Example",
                "category": "macro",
            },
        ]
        return {"results": [{"source": "Example", "articles": articles}]}

    def test_valid_fresh_report_passes(self):
        with writable_temporary_directory() as directory:
            path = Path(directory) / "report.json"
            path.write_text(
                json.dumps(self.make_report(datetime.now(timezone.utc).isoformat())),
                encoding="utf-8",
            )
            summary = report_health.validate_report(
                path, min_sources=1, min_articles=2, max_age_minutes=5
            )
            self.assertEqual(summary["unique_urls"], 2)

    def test_duplicate_or_stale_report_fails(self):
        with writable_temporary_directory() as directory:
            path = Path(directory) / "report.json"
            old = (datetime.now(timezone.utc) - timedelta(hours=4)).isoformat()
            path.write_text(
                json.dumps(self.make_report(old, duplicate=True)), encoding="utf-8"
            )
            with self.assertRaises(report_health.ReportHealthError):
                report_health.validate_report(
                    path, min_sources=1, min_articles=2, max_age_minutes=5
                )


class DigestDeliveryTests(unittest.TestCase):
    def test_twilio_delivery_uses_whatsapp_addresses(self):
        response = FakeResponse(201, payload={"sid": "SM123"})
        env = {
            "TWILIO_ACCOUNT_SID": "AC123",
            "TWILIO_AUTH_TOKEN": "token",
            "TWILIO_WHATSAPP_FROM": "+15550000001",
            "TWILIO_WHATSAPP_TO": "+15550000002",
        }
        with patch.dict(os.environ, env, clear=True), patch.object(
            digest.requests, "post", return_value=response
        ) as post:
            digest.send_twilio("hello")
        payload = post.call_args.kwargs["data"]
        self.assertEqual(payload["From"], "whatsapp:+15550000001")
        self.assertEqual(payload["To"], "whatsapp:+15550000002")
        self.assertEqual(payload["Body"], "hello")

    def test_missing_delivery_can_be_detected(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(digest.configured_delivery(), "none")


class WorkflowContractTests(unittest.TestCase):
    def test_owned_workflow_has_schedule_timezone_sync_and_write_scope(self):
        workflow = (ROOT / ".github/workflows/financial-pipeline.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn('timezone: "America/New_York"', workflow)
        self.assertIn("contents: write", workflow)
        self.assertIn("scripts/pipeline.py", workflow)
        self.assertIn("NOTION_API_KEY", workflow)
        self.assertIn("git push origin HEAD:main", workflow)


if __name__ == "__main__":
    unittest.main()
