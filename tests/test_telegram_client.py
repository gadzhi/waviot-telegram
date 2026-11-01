import logging

import httpx
import pytest

from waviot_telegram.telegram_client import TelegramClient, TelegramError


def test_token_is_absent_from_http_logs_and_errors(caplog):
    token = "123456:synthetic-telegram-token"
    caplog.set_level(logging.DEBUG)
    transport = httpx.MockTransport(lambda request: httpx.Response(429, json={"ok": False, "error_code": 429, "description": "Too Many Requests", "parameters": {"retry_after": 12}}))
    with TelegramClient(token, httpx.Client(transport=transport)) as client:
        with pytest.raises(TelegramError) as error:
            client.call("getMe")
    assert error.value.retry_after == 12
    assert token not in caplog.text + str(error.value)
