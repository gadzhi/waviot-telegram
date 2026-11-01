from decimal import Decimal
import logging

import httpx
import pytest

from waviot_telegram.waviot_client import WaviotClient, WaviotError


def client_for(handler):
    return WaviotClient("secret-test-key", httpx.Client(transport=httpx.MockTransport(handler)))


def test_read_only_request_keeps_hex_id_and_decimal_precision():
    def handler(request):
        assert request.method == "GET"
        assert request.url.host == "lk.waviot.ru"
        assert request.url.params["key"] == "secret-test-key"
        assert request.url.params["modem_id"] == "00ABCD"
        return httpx.Response(200, text='{"status":"ok","channel":"17","values":{"1700000000":0.060000000000000001,"1700003600":null}}')

    with client_for(handler) as client:
        data = client.read("channel_values", modem_id="00ABCD", channel="17", **{"from": 1700000000, "to": 1700003600})
    assert data["values"]["1700000000"] == Decimal("0.060000000000000001")
    assert data["values"]["1700003600"] is None


@pytest.mark.parametrize("body", [
    '{"status":"error","message":"secret-test-key"}',
    '{"status":"ok"}',
    '{"status":"ok","values":[],"channel":"17"}',
    '{"status":"ok","values":{"1700000000":false},"channel":"17"}',
    '{"status":"ok","values":{"1700000000":"NaN"},"channel":"17"}',
    '{"status":"ok","values":{},"channel":"99"}',
    'not json secret-test-key',
])
def test_invalid_responses_are_errors_not_zero_consumption(body):
    with client_for(lambda request: httpx.Response(200, text=body)) as client:
        with pytest.raises(WaviotError) as caught:
            client.read("channel_values", modem_id="00ABCD", channel="17", **{"from": 1, "to": 2})
    assert "secret-test-key" not in str(caught.value)


def test_timeout_does_not_echo_credential_url():
    def handler(request):
        raise httpx.ReadTimeout(str(request.url), request=request)

    with client_for(handler) as client:
        with pytest.raises(WaviotError, match="timeout") as caught:
            client.read("tree")
    assert "secret-test-key" not in str(caught.value)


def test_redirect_is_not_followed_with_api_key():
    with client_for(lambda request: httpx.Response(302, headers={"location": "https://example.com/"})) as client:
        with pytest.raises(WaviotError, match="HTTP 302"):
            client.read("tree")


def test_non_read_only_method_never_reaches_transport():
    def handler(request):
        pytest.fail("unapproved endpoint reached the network")

    with client_for(handler) as client:
        with pytest.raises(ValueError):
            client.read("set_tariff")


def test_http_client_logging_does_not_expose_query_key(caplog):
    caplog.set_level(logging.INFO, logger="httpx")
    with client_for(lambda request: httpx.Response(200, json={"status": "ok", "tree": {}})) as client:
        client.read("tree")
    assert "secret-test-key" not in caplog.text
