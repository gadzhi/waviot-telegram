"""Local configuration without shell evaluation or secret-bearing diagnostics."""

import math
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping


@dataclass(frozen=True)
class Settings:
    api_key: str = field(default="", repr=False)
    house_id: str | None = None
    request_interval: float = 1.0
    timeout: float = 30.0
    house_label: str = "Подключённый дом"


def node_id(value: str) -> str:
    if not value.isascii() or not value.isdecimal() or int(value) <= 0:
        raise ValueError("Node ID must be a positive integer.")
    return value


def load_environment(path: Path = Path(".env"), environ: Mapping[str, str] | None = None) -> dict:
    values = {}
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                raise ValueError("Invalid .env: expected KEY=VALUE; shell syntax is unsupported.")
            key, value = line.split("=", 1)
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            values[key.strip()] = value
    values.update(os.environ if environ is None else environ)
    return values


def load_settings(path: Path = Path(".env"), environ: Mapping[str, str] | None = None) -> Settings:
    values = load_environment(path, environ)
    try:
        interval = float(values.get("WAVIOT_REQUEST_INTERVAL_SECONDS", "1"))
        timeout = float(values.get("WAVIOT_TIMEOUT_SECONDS", "30"))
    except ValueError:
        raise ValueError("Request interval and timeout must be numbers.") from None
    if not math.isfinite(interval) or interval < 0 or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("Request interval must be nonnegative and timeout must be positive.")
    house = values.get("WAVIOT_HOUSE_ID", "").strip()
    return Settings(
        api_key=values.get("WAVIOT_API_KEY", "").strip(),
        house_id=node_id(house) if house else None,
        request_interval=interval,
        timeout=timeout,
        house_label=" ".join(values.get("WAVIOT_HOUSE_LABEL", "Подключённый дом").split())[:100] or "Подключённый дом",
    )
