import asyncio
import unittest

import app.main as main
from app.cache import PageCache, TokenEntry


class CacheViewIsolationTests(unittest.TestCase):
    def test_processed_data_does_not_replace_raw_activity_cache(self):
        entry = TokenEntry("u1", "token", {"id": "u1"}, 0.0)
        original_cache = main.page_cache
        original_client = main.DreamClient
        calls = {"count": 0}

        class FakeDreamClient:
            def __init__(self, token):
                self.token = token

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return None

            async def fetch_page_one(self, endpoint, user_id, page, page_size):
                calls["count"] += 1
                return ([{
                    "id": "A1",
                    "name": "原始活动",
                    "status": "6",
                    "raw_only": "must-survive",
                }], 1)

        async def scenario():
            main.page_cache = PageCache(ttl=60)
            main.DreamClient = FakeDreamClient
            processed = await main.get_data("join", 1, 10, False, entry)
            raw = await main.get_activities("join", 1, 10, False, entry)
            return processed, raw

        try:
            processed, raw = asyncio.run(scenario())
        finally:
            main.page_cache = original_cache
            main.DreamClient = original_client

        self.assertEqual(calls["count"], 1)
        self.assertEqual(processed["records"][0]["活动名称"], "原始活动")
        self.assertEqual(raw["records"][0]["raw_only"], "must-survive")
        self.assertEqual(raw["records"][0]["name"], "原始活动")
        self.assertNotIn("活动名称", raw["records"][0])


if __name__ == "__main__":
    unittest.main()
