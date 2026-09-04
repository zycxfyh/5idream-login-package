import asyncio
import unittest

import app.main as main
from app.cache import LoginSessionStore, PersonalTokenCache
from app.client import DreamAPIError


class FakeAsyncClient:
    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None


class FakeDreamClient:
    def __init__(self, token):
        self.token = token

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    async def check_token(self):
        return {"id": "u1", "name": "user"}


class LoginPollRaceTests(unittest.TestCase):
    def test_late_poll_error_cannot_overwrite_success(self):
        original_store = main.session_store
        original_cache = main.token_cache
        original_async_client = main.httpx.AsyncClient
        original_dream_client = main.DreamClient
        original_poll = main.poll_login_once

        store = LoginSessionStore(ttl=60)
        cache = PersonalTokenCache(idle_ttl=60)
        session = store.create("security", "payload")
        release_first = asyncio.Event()
        second_started = asyncio.Event()
        calls = {"count": 0}

        async def fake_poll(client, security_id):
            calls["count"] += 1
            if calls["count"] == 1:
                await release_first.wait()
                for _ in range(5):
                    await asyncio.sleep(0)
                return "token-1"
            while session.status != "success":
                await asyncio.sleep(0)
            raise DreamAPIError("synthetic late poll failure")

        async def second_call():
            second_started.set()
            return await main.login_status(session.session_id)

        async def scenario():
            first = asyncio.create_task(main.login_status(session.session_id))
            await asyncio.sleep(0)
            second = asyncio.create_task(second_call())
            await second_started.wait()
            release_first.set()
            return await asyncio.gather(first, second)

        main.session_store = store
        main.token_cache = cache
        main.httpx.AsyncClient = FakeAsyncClient
        main.DreamClient = FakeDreamClient
        main.poll_login_once = fake_poll
        try:
            results = asyncio.run(scenario())
        finally:
            main.session_store = original_store
            main.token_cache = original_cache
            main.httpx.AsyncClient = original_async_client
            main.DreamClient = original_dream_client
            main.poll_login_once = original_poll

        self.assertEqual(session.status, "success")
        self.assertEqual(session.token, "token-1")
        self.assertTrue(all(result["status"] == "success" for result in results))
        self.assertEqual(calls["count"], 1)


if __name__ == "__main__":
    unittest.main()
