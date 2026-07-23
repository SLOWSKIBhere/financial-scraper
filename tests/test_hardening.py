import importlib.util
import os
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]


class FakeResponse:
    status_code = 200
    headers = {}
    ok = True
    text = ""

    def json(self):
        return {"results": []}

    def raise_for_status(self):
        return None


class FakeRequests(types.ModuleType):
    def __init__(self):
        super().__init__("requests")
        self.calls = []

    def request(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return FakeResponse()

    def post(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return FakeResponse()


def load_script(name, relative_path, fake_requests):
    fake_dotenv = types.ModuleType("dotenv")
    fake_dotenv.load_dotenv = lambda *args, **kwargs: None
    path = ROOT / relative_path
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    with patch.dict(
        sys.modules,
        {"requests": fake_requests, "dotenv": fake_dotenv},
    ):
        spec.loader.exec_module(module)
    return module


class HardeningTests(unittest.TestCase):
    def test_collector_keeps_tls_verification_enabled(self):
        source = (ROOT / "collect.py").read_text(encoding="utf-8")
        self.assertNotIn("verify=False", source)
        compile(source, "collect.py", "exec")

    def test_local_sync_accepts_canonical_and_legacy_token_names(self):
        module = load_script(
            "sync_to_notion_test",
            "scripts/sync_to_notion.py",
            FakeRequests(),
        )
        with patch.dict(
            os.environ,
            {"NOTION_TOKEN": "canonical", "NOTION_ACCESS_TOKEN": "legacy"},
            clear=True,
        ):
            self.assertEqual(module.get_notion_token(), "canonical")
        with patch.dict(
            os.environ,
            {"NOTION_ACCESS_TOKEN": "legacy"},
            clear=True,
        ):
            self.assertEqual(module.get_notion_token(), "legacy")

    def test_local_sync_sets_request_timeout(self):
        fake_requests = FakeRequests()
        module = load_script(
            "sync_to_notion_timeout_test",
            "scripts/sync_to_notion.py",
            fake_requests,
        )
        module.notion_request("GET", "databases/example", "token")
        self.assertEqual(
            fake_requests.calls[0][1]["timeout"],
            module.REQUEST_TIMEOUT_SECONDS,
        )

    def test_sandbox_sync_sets_timeouts_on_query_and_create(self):
        fake_requests = FakeRequests()
        module = load_script(
            "notion_sync_sandbox_test",
            "scripts/notion_sync_sandbox.py",
            fake_requests,
        )
        module.get_existing_urls()
        module.push_article(
            {
                "url": "https://example.com/article",
                "title": "Example",
                "summary": "Summary",
            },
            "RSS",
            set(),
        )
        self.assertEqual(len(fake_requests.calls), 2)
        self.assertTrue(
            all(
                kwargs["timeout"] == module.REQUEST_TIMEOUT_SECONDS
                for _, kwargs in fake_requests.calls
            )
        )

    def test_digest_score_is_bounded_and_can_reach_ten(self):
        module = load_script(
            "daily_digest_test",
            "scripts/daily_digest.py",
            FakeRequests(),
        )
        score = module.score_article(
            "$AAPL surge rally after earnings beat",
            "A detailed summary of the quarterly report and guidance.",
            "earnings",
            "Reuters",
        )
        self.assertEqual(score, 10)
        self.assertLessEqual(
            module.score_article(
                "$AAPL crash surge rally plunge soar beat miss cut hike halt ban",
                "A detailed summary with many impact words.",
                "earnings",
                "Reuters",
            ),
            10,
        )


if __name__ == "__main__":
    unittest.main()
