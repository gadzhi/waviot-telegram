import logging

import httpx


class TelegramError(Exception):
    def __init__(self, code, *, retry_after=None, uncertain=False, description=""):
        super().__init__(f"Telegram error {code}" + (f": {description}" if description else ""))
        self.code = code
        self.retry_after = retry_after
        self.uncertain = uncertain


class _TokenFilter(logging.Filter):
    def __init__(self, token):
        super().__init__()
        self.token = token

    def filter(self, record):
        record.msg = record.getMessage().replace(self.token, "[redacted]")
        record.args = ()
        return True


class TelegramClient:
    METHODS = {"getMe", "getWebhookInfo", "getUpdates", "sendMessage", "editMessageText", "answerCallbackQuery",
               "setMyCommands", "getMyCommands", "setChatMenuButton", "getChatMenuButton", "getChatAdministrators"}

    def __init__(self, token, http_client=None):
        if not token:
            raise ValueError("Добавьте TELEGRAM_BOT_TOKEN в локальный .env.")
        self._token = token
        self._http = http_client or httpx.Client(timeout=40, follow_redirects=False)
        self._owns_http = http_client is None
        self._filter = _TokenFilter(token)
        self._loggers = [logging.getLogger("httpx"), logging.getLogger("httpcore.http11")]
        for logger in self._loggers:
            logger.addFilter(self._filter)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        if self._owns_http:
            self._http.close()
        for logger in self._loggers:
            logger.removeFilter(self._filter)

    def call(self, method, **payload):
        if method not in self.METHODS:
            raise ValueError("Unsupported Telegram method.")
        try:
            response = self._http.post(f"https://api.telegram.org/bot{self._token}/{method}", json=payload, follow_redirects=False)
            data = response.json()
        except httpx.HTTPError:
            raise TelegramError("network", uncertain=method in {"sendMessage", "editMessageText"}) from None
        except ValueError:
            raise TelegramError("invalid_json", uncertain=method in {"sendMessage", "editMessageText"}) from None
        if not isinstance(data, dict) or not data.get("ok"):
            code = data.get("error_code", response.status_code) if isinstance(data, dict) else response.status_code
            parameters = data.get("parameters", {}) if isinstance(data, dict) else {}
            description = str(data.get("description", ""))[:200].replace(self._token, "[redacted]") if isinstance(data, dict) else ""
            raise TelegramError(code, retry_after=parameters.get("retry_after"), uncertain=response.status_code >= 500, description=description)
        return data["result"]
