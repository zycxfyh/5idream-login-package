import asyncio
import unittest

import httpx

from app.client import DreamAPIError, DreamClient


class PageResponseShapeTests(unittest.TestCase):
    def run_with_payload(self, payload, *, all_pages=False):
        async def scenario():
            client = DreamClient("synthetic-token")

            async def fake_request(method, url, **kwargs):
                request = httpx.Request(method, url)
                return httpx.Response(200, request=request, json=payload)

            client._request = fake_request
            try:
                if all_pages:
                    return await client.fetch_page("/synthetic", "user-1", 10)
                return await client.fetch_page_one("/synthetic", "user-1", 1, 10)
            finally:
                await client.aclose()

        return asyncio.run(scenario())

    def test_page_one_rejects_object_rows_instead_of_returning_empty_page(self):
        with self.assertRaises(DreamAPIError):
            self.run_with_payload({"rows": {"unexpected": "object"}, "records": 1})

    def test_full_fetch_rejects_object_rows_instead_of_silently_dropping_records(self):
        with self.assertRaises(DreamAPIError):
            self.run_with_payload({"rows": {"unexpected": "object"}, "records": 1}, all_pages=True)

    def test_empty_rows_remain_valid(self):
        records, total = self.run_with_payload({"rows": [], "records": 0})
        self.assertEqual(records, [])
        self.assertEqual(total, 0)


if __name__ == "__main__":
    unittest.main()
