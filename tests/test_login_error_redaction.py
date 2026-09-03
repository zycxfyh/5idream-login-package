import asyncio

import httpx
import pytest

import app.main as main


def test_token_validation_http_error_does_not_reflect_token_to_client() -> None:
    secret = 'SECRET-TOKEN-MUST-NOT-LEAK'

    async def fake_poll_login_once(client, security_id):
        return secret

    class FailingDreamClient:
        def __init__(self, token: str):
            assert token == secret

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return None

        async def check_token(self):
            request = httpx.Request(
                'GET',
                'https://www.5idream.net/token/checkToken',
                params={'token': secret},
            )
            response = httpx.Response(401, request=request)
            response.raise_for_status()

    async def scenario() -> dict:
        session = main.session_store.create('security-id', 'qr-payload')
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(main, 'poll_login_once', fake_poll_login_once)
            mp.setattr(main, 'DreamClient', FailingDreamClient)
            return await main.login_status(session.session_id)

    result = asyncio.run(scenario())
    assert result['status'] == 'failed'
    assert secret not in result['error']
