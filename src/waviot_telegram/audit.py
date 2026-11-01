"""Collect observations; do not diagnose meters or certify live activation."""

import json
import os
import re
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from .config import Settings, node_id
from .waviot_client import WaviotClient, WaviotError, counter_value


def tree_node_ids(tree) -> set[str]:
    found = set()
    if isinstance(tree, list):
        for item in tree:
            found.update(tree_node_ids(item))
    elif isinstance(tree, dict):
        if "id" in tree and ("type" in tree or "name" in tree):
            try:
                found.add(node_id(str(tree["id"])))
            except ValueError:
                pass
        for child in tree.values():
            if isinstance(child, (dict, list)):
                found.update(tree_node_ids(child))
    return found


def validate_scope(scope: dict, house_id: str | None) -> list[str]:
    if not isinstance(scope, dict) or scope.get("house_id") != house_id or not house_id:
        raise ValueError("Expected scope house_id must match configured WAVIOT_HOUSE_ID.")
    expected = scope.get("expected_node_ids")
    if not isinstance(expected, list) or not expected:
        raise ValueError("Expected scope must list verified expected_node_ids.")
    return sorted({node_id(str(value)) for value in expected})


def channel_summary(values: dict) -> dict:
    usable = sorted((int(ts), counter_value(value)) for ts, value in values.items() if value is not None)
    gaps = [right[0] - left[0] for left, right in zip(usable, usable[1:])]
    return {
        "usable_values": len(usable),
        "missing_values": sum(value is None for value in values.values()),
        "first_measurement_timestamp": usable[0][0] if usable else None,
        "last_measurement_timestamp": usable[-1][0] if usable else None,
        "observed_gap_seconds_min": min(gaps) if gaps else None,
        "observed_gap_seconds_max": max(gaps) if gaps else None,
        "timestamp_semantics_verified": False,
        "units_and_resolution_verified": False,
    }


def collect_audit(
    settings: Settings,
    client: WaviotClient,
    *,
    element_ids: list[str] | None = None,
    modem_id: str | None = None,
    channel: str | None = None,
    days: int = 7,
    expected_scope: dict | None = None,
    include_balance: bool = False,
    now: datetime | None = None,
) -> dict:
    if not 1 <= days <= 90:
        raise ValueError("History window must be between 1 and 90 days.")
    if bool(modem_id) != (channel is not None):
        raise ValueError("Specify modem ID and channel together.")
    if modem_id and not re.fullmatch(r"[0-9A-Fa-f]{1,32}", modem_id):
        raise ValueError("Modem ID must be a HEX string.")
    if channel is not None and not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", channel):
        raise ValueError("Channel ID must be a numeric or named ASCII identifier.")
    elements = [node_id(str(value)) for value in element_ids or []]
    expected = validate_scope(expected_scope, settings.house_id) if expected_scope is not None else None
    if include_balance and not settings.house_id:
        raise ValueError("Balance comparison requires WAVIOT_HOUSE_ID.")
    now = now or datetime.now(timezone.utc)
    if now.utcoffset() is None:
        raise ValueError("Audit time must include a timezone.")
    start = int((now - timedelta(days=days)).timestamp())
    end = int(now.timestamp())
    report = {
        "collected_at": now.astimezone(timezone.utc).isoformat(),
        "house_id": settings.house_id,
        "requested_period": {"from": start, "to": end, "days": days},
        "live_alerts_ready": False,
        "coverage": {"status": "unverified", "missing_node_ids": []},
        "responses": {},
        "errors": [],
        "channel_summary": None,
        "packet_observation": None,
        "unverified": [
            "Full house coverage and device/channel mapping require independent verification.",
            "Measurement timestamp semantics, units, resolution and interpolation are unverified.",
            "Observed timestamp gaps are not a verified reporting schedule.",
            "Battery thresholds and event-code meanings require gas-model documentation.",
            "No anomaly classification or Telegram delivery is performed.",
        ],
    }
    tree_params = {"fields": "appartment,street,building,entrance"}
    if settings.house_id:
        tree_params["id"] = settings.house_id
    calls = [("tree", tree_params)]
    calls.extend(("element", {"id": element}) for element in elements)
    if modem_id:
        calls.extend([
            ("modem", {"id": modem_id}),
            ("modem_history", {"id": modem_id}),
            ("last_packet", {"id": modem_id}),
            ("channel_values", {"modem_id": modem_id, "channel": channel, "from": start, "to": end}),
            ("events", {"modem_id": modem_id, "from": start, "to": end}),
        ])
    if include_balance:
        calls.append(("balance", {"elementId": settings.house_id, "from": start, "to": end}))
    for index, (endpoint, params) in enumerate(calls):
        if index:
            time.sleep(settings.request_interval)
        label = f"element_{params['id']}" if endpoint == "element" else endpoint
        try:
            data = client.read(endpoint, **params)
        except WaviotError as error:
            report["errors"].append({"endpoint": label, "code": error.code, "message": str(error)})
            if endpoint == "tree" or error.code == "rate_limited":
                break
            continue
        report["responses"][label] = data
        if endpoint == "tree":
            observed = sorted(tree_node_ids(data["tree"]))
            report["coverage"]["observed_node_ids"] = observed
            if expected is not None:
                missing = sorted(set(expected) - set(observed))
                report["coverage"].update(status="missing" if missing else "expected_nodes_found", missing_node_ids=missing)
        elif endpoint == "channel_values":
            report["channel_summary"] = channel_summary(data["values"])
        elif endpoint == "last_packet":
            report["packet_observation"] = data["packet"]
    return report


PRIVATE_FIELDS = {
    "key", "api_key", "token", "access_token", "refresh_token", "authorization",
    "cookie", "password", "secret", "firstname", "lastname", "middlename", "fio",
    "full_name", "resident_name", "email", "phone", "account", "name",
}


def redact(value, secrets: tuple[str, ...]):
    if isinstance(value, dict):
        cleaned = {}
        for key, child in value.items():
            field = str(key).lower()
            sensitive = field in PRIVATE_FIELDS or field.endswith(("_key", "_token", "_password"))
            cleaned[key] = "[redacted]" if sensitive else redact(child, secrets)
        return cleaned
    if isinstance(value, list):
        return [redact(item, secrets) for item in value]
    if isinstance(value, str):
        for secret in secrets:
            if secret:
                value = value.replace(secret, "[redacted]")
        return value
    return value


def render_report(report: dict) -> str:
    coverage = report.get("coverage", {})
    lines = [
        "# Аудит данных Waviot",
        "",
        "Статус: сбор наблюдений. Рабочие уведомления не разрешены этим отчётом.",
        "",
        f"Время сбора UTC: {report.get('collected_at', 'не указано')}",
        f"Охват: {coverage.get('status', 'unverified')}. Даже найденные ожидаемые узлы не доказывают полноту всего дома.",
        f"Отсутствующие ожидаемые узлы: {', '.join(coverage.get('missing_node_ids', [])) or 'не обнаружены / список не задан'}.",
        "",
        "## Выполненные запросы",
        "",
    ]
    lines.extend(f"- {endpoint}: ответ получен; содержание требует проверки." for endpoint in report.get("responses", {}))
    lines.extend(f"- {error['endpoint']}: {error['code']} — {error['message']}" for error in report.get("errors", []))
    lines.extend(["", "## Измерения выбранного канала", ""])
    summary = report.get("channel_summary")
    if summary is None:
        lines.append("Нет пригодной выборки. Это не нулевой расход.")
    else:
        lines.extend([
            f"- Пригодных значений: {summary['usable_values']}; пропусков: {summary['missing_values']}.",
            f"- Последняя метка значения: {summary['last_measurement_timestamp']}; смысл метки ещё не подтверждён.",
            "- Пакет модема сохранён отдельно; его получение не подтверждает свежесть газового измерения.",
        ])
    lines.extend(["", "## Непроверенные условия", ""])
    lines.extend(f"- {item}" for item in report.get("unverified", []))
    return "\n".join(lines) + "\n"


def write_audit(report: dict, output: Path, *, secrets: tuple[str, ...] = ()) -> None:
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    cleaned = redact(report, secrets)
    evidence = json.dumps(cleaned, ensure_ascii=False, indent=2, default=str, allow_nan=False)
    for name, content in [("evidence.json", evidence + "\n"), ("report.md", render_report(cleaned))]:
        with open(output / name, "x", encoding="utf-8", opener=lambda path, flags: os.open(path, flags, 0o600)) as stream:
            stream.write(content)
