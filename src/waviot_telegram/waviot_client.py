"""Only explicitly listed read-only API endpoints are callable."""

import json
import logging
import re
from decimal import Decimal, InvalidOperation

import httpx


ENDPOINTS = {
    "tree": ("api.tree/get_tree/", "tree"),
    "element": ("api.data/get_full_element_info/", "devices"),
    "modem": ("api.modem/info/", "modem"),
    "modem_history": ("api.modem/history/", "history"),
    "last_packet": ("api.modem/get_last_message/", "packet"),
    "channel_values": ("api.data/get_modem_channel_values/", "values"),
    "events": ("api.data/get_events/", "events"),
    "balance": ("api.data/get_balance_info/", "balance"),
}


class WaviotError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class _RequestLogRedactor(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = re.sub(r"([?&]key=)[^&\s'\"<>]+", r"\1[redacted]", record.getMessage())
        record.args = ()
        return True


def counter_value(value) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise ValueError("Unsupported counter value.")
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        raise ValueError("Invalid counter value.") from None
    if not number.is_finite():
        raise ValueError("Counter value must be finite.")
    return number


class WaviotClient:
    def __init__(self, api_key: str, http_client: httpx.Client | None = None, *, timeout: float = 30):
        if not api_key:
            raise ValueError("WAVIOT_API_KEY is not configured.")
        self._key = api_key
        self._http = http_client if http_client is not None else httpx.Client(timeout=timeout, follow_redirects=False)
        self._owns_http = http_client is None
        self._log_filter = _RequestLogRedactor()
        self._loggers = [logging.getLogger("httpx"), logging.getLogger("httpcore.http11")]
        for logger in self._loggers:
            logger.addFilter(self._log_filter)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        if self._owns_http:
            self._http.close()
        for logger in self._loggers:
            logger.removeFilter(self._log_filter)

    def read(self, endpoint: str, **params) -> dict:
        if endpoint not in ENDPOINTS:
            raise ValueError("Endpoint is not on the read-only allowlist.")
        if "key" in params:
            raise ValueError("Credentials cannot be overridden by request parameters.")
        path, field = ENDPOINTS[endpoint]
        try:
            response = self._http.get(
                "https://lk.waviot.ru/" + path,
                params={**params, "key": self._key},
                follow_redirects=False,
            )
        except httpx.TimeoutException:
            raise WaviotError("timeout", "Waviot request timeout.") from None
        except httpx.HTTPError:
            raise WaviotError("network_error", "Waviot network request failed.") from None
        if response.status_code != 200:
            code = "rate_limited" if response.status_code == 429 else "http_error"
            raise WaviotError(code, f"Waviot returned HTTP {response.status_code}.")
        try:
            data = json.loads(response.text, parse_float=Decimal)
        except (ValueError, UnicodeError):
            raise WaviotError("invalid_json", "Waviot returned invalid JSON.") from None
        if not isinstance(data, dict) or data.get("status") != "ok":
            raise WaviotError("api_error", "Waviot did not return status=ok.")
        if field not in data or not isinstance(data[field], (list, dict)):
            raise WaviotError("invalid_schema", f"Waviot response has no supported {field} structure.")
        if endpoint == "channel_values":
            if not isinstance(data["values"], dict) or str(data.get("channel")) != str(params.get("channel")):
                raise WaviotError("invalid_schema", "Waviot returned an unexpected channel structure.")
            try:
                for timestamp, value in data["values"].items():
                    if not timestamp.isascii() or not timestamp.isdecimal() or int(timestamp) <= 0:
                        raise ValueError("Unsupported measurement timestamp.")
                    counter_value(value)
            except ValueError:
                raise WaviotError("invalid_schema", "Waviot returned invalid measurement timestamps or values.") from None
        return data
