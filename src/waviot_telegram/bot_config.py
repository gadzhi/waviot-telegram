import re
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo

from .config import load_environment


@dataclass(frozen=True)
class BotSettings:
    token: str = field(repr=False)
    database: Path = Path("artifacts/runtime/state.sqlite3")
    timezone: str = "Europe/Moscow"


def load_bot_settings():
    values = load_environment()
    token = values.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise ValueError("Добавьте TELEGRAM_BOT_TOKEN в локальный .env. Не передавайте токен в чат.")
    if not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]+", token):
        raise ValueError("Некорректный формат TELEGRAM_BOT_TOKEN; проверьте значение из BotFather.")
    timezone = values.get("BOT_TIMEZONE", "Europe/Moscow")
    ZoneInfo(timezone)
    if values.get("BOT_LIVE_ALERTS", "false").lower() not in {"false", "0", ""}:
        raise ValueError("Автоматические тревоги пока недоступны: контракт потребления газа и воды не подтверждён.")
    return BotSettings(token, Path(values.get("BOT_DATABASE", "artifacts/runtime/state.sqlite3")), timezone)
