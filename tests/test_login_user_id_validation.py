import asyncio
import unittest

import app.main as main
from app.cache import LoginSessionStore, PersonalTokenCache


class LoginUserIdValidationTests(unittest.TestCase):
    def run_login(self, user):
        original_store = main.session_store
        original_cache = main.token_cache
        original_poll = main.poll_login_once
        original_client = main.DreamClient

        class FakeDreamClient:
            def __init__(self, token):
                self.token = token

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return None

            async def check_token(self):
                return user

        async def fake_poll(client, security_id):
            return "synthetic-token"

        async def scenario():
            main.session_store = LoginSessionStore(ttl=1800)
            main.token_cache = PersonalTokenCache(idle_ttl=3600)
            main.poll_login_once = fake_poll
            main.DreamClient = FakeDreamClient
            session = main.session_store.create("security", "qr")
            result = await main.login_status(session.session_id)
            cached = main.token_cache.get_by_token("synthetic-token")
            return result, cached

        try:
            return asyncio.run(scenario())
        finally:
            main.session_store = original_store
            main.token_cache = original_cache
            main.poll_login_once = original_poll
            main.DreamClient = original_client

    def test_missing_user_id_does_not_return_success_token(self):
        result, cached = self.run_login({"name": "synthetic-user"})
        self.assertEqual(result["status"], "failed")
        self.assertNotIn("token", result)
        self.assertIsNone(cached)

    def test_blank_user_id_does_not_return_success_token(self):
        result, cached = self.run_login({"id": "   ", "name": "synthetic-user"})
        self.assertEqual(result["status"], "failed")
        self.assertIsNone(cached)

    def test_valid_user_id_returns_usable_cached_token(self):
        result, cached = self.run_login({"id": "user-1", "name": "synthetic-user"})
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["token"], "synthetic-token")
        self.assertIsNotNone(cached)
        self.assertEqual(cached.user_id, "user-1")


if __name__ == "__main__":
    unittest.main()
