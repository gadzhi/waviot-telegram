"""Whole-house metadata collection. Unverified usage never becomes an alert."""

import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from .audit import tree_node_ids
from .config import load_settings
from .storage import Store
from .waviot_client import WaviotClient, WaviotError, counter_value


def inventory_row(identity, response, observed_at):
    result = {key: identity[key] for key in ("element_id", "corpus", "apartment")}
    result.update(resources=[], gas=[], observed_at=observed_at)
    devices = response.get("devices", {})
    for device in devices.values() if isinstance(devices, dict) else devices:
        resource = device.get("class_name")
        if resource in ("Gas", "Water", "Electricity") and resource not in result["resources"]:
            result["resources"].append(resource)
        if resource == "Gas":
            registers = device.get("registrators", {})
            gas_register = next((r for key, r in registers.items() if key == "gas" or str(r.get("channel_id")) == "1053"), {})
            timestamp = gas_register.get("last_value_timestamp")
            try:
                timestamp = int(timestamp) if timestamp is not None and not isinstance(timestamp, bool) else None
            except (ValueError, TypeError):
                timestamp = None
            try:
                value = counter_value(gas_register.get("last_value"))
            except ValueError:
                value = None
            result["gas"].append({"gas_id": str(device["id"]), "modem_id": device.get("modem_id"), "last_timestamp": timestamp,
                                  "meter_sn": device.get("device_sn"), "last_value": str(value) if value is not None else None, "unit": gas_register.get("unit_id")})
    return result


def stale_candidates(inventory, now):
    results = []
    for row in inventory:
        if row.get("error"):
            continue
        for gas in row.get("gas", []):
            timestamp = gas.get("last_timestamp")
            if timestamp and 0 < timestamp <= now.timestamp() - 72 * 3600:
                last = datetime.fromtimestamp(timestamp, ZoneInfo("Europe/Moscow")).strftime("%d.%m.%Y %H:%M")
                results.append({"gas_id": gas["gas_id"], "label": f"{row['corpus']}, кв. {row['apartment']}", "last_at": last,
                                "meter_snapshot": case_snapshot(row, gas),
                                "evidence": f"Старая метка газового значения API: {last} (Europe/Moscow). Метаданные получены {row['observed_at']}. Это не подтверждение нулевого потребления. Причина не установлена."})
    return results


def case_snapshot(row, gas):
    return {"apartment": row["apartment"], "corpus": row["corpus"], "meter_sn": gas.get("meter_sn"),
            "last_value": gas.get("last_value"), "unit": gas.get("unit"), "last_timestamp": gas.get("last_timestamp"), "observed_at": row.get("observed_at")}


def demo_snapshot(now):
    return {"apartment": "учебная", "corpus": "Тестовые данные", "meter_sn": "DEMO-GAS-001", "last_value": "123.450", "unit": "m³",
            "last_timestamp": int((now - timedelta(days=4)).timestamp()), "observed_at": now.isoformat()}


def refresh_case_snapshots(store):
    inventory = store.get("inventory", [])
    for item in store.incidents():
        if item["demo"]:
            if not item["meter_snapshot"]:
                store.update_meter_snapshot(item["id"], demo_snapshot(datetime.fromisoformat(item["created_at"])))
            continue
        for row in inventory:
            if row.get("error"):
                continue
            gas = next((gas for gas in row.get("gas", []) if gas["gas_id"] == item["gas_id"]), None)
            if gas:
                store.update_meter_snapshot(item["id"], case_snapshot(row, gas))
                break


def load_seed(store, path=Path("artifacts/full-inventory.jsonl")):
    if store.get("inventory") or not path.exists():
        return
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    projected = [inventory_row(row, row["response"], row["observed_at"]) for row in rows]
    store.set("inventory", projected)
    store.set("inventory_source", "Сохранённый аудит; время получения указано у каждого кандидата. Обновление: /scan.")


def apartment_nodes(tree):
    rows = []
    def visit(value, corpus="Корпус не указан"):
        if isinstance(value, dict):
            name = str(value.get("name", ""))
            if re.match(r"Корпус\s+\d+", name):
                corpus = name[:100]
            if value.get("type") == "apt":
                apartment = value.get("appartment") or name.removeprefix("Квартира ").removeprefix("кв. ")
                rows.append({"element_id": str(value["id"]), "corpus": corpus, "apartment": str(apartment)[:100]})
            for child in value.values():
                if isinstance(child, (dict, list)):
                    visit(child, corpus)
        elif isinstance(value, list):
            for child in value:
                visit(child, corpus)
    visit(tree)
    return rows


def collect_inventory(database, stop):
    settings = load_settings()
    now = lambda: datetime.now(timezone.utc).isoformat()
    with Store(database) as store:
        status = {"state": "running", "processed": 0, "total": len(store.get("inventory", [])), "started_at": now()}
        store.set("scan_status", status)
        try:
            if not settings.house_id:
                raise ValueError("Не задан WAVIOT_HOUSE_ID.")
            with WaviotClient(settings.api_key, timeout=settings.timeout) as client:
                tree = client.read("tree", id=settings.house_id, fields="appartment,street,building,entrance")["tree"]
                rows = apartment_nodes(tree)
                scope_path = Path(os.environ.get("WAVIOT_SCOPE_FILE", "audit-scope.json"))
                scope = json.loads(scope_path.read_text()) if scope_path.exists() else {}
                expected = scope.get("accepted_apartment_count")
                if type(expected) is not int or expected <= 0:
                    raise ValueError("Укажите проверенное число квартир accepted_apartment_count в audit-scope.json.")
                required = set(map(str, scope.get("expected_node_ids", [])))
                if len(rows) != expected or len({r['element_id'] for r in rows}) != len(rows) or not required <= tree_node_ids(tree):
                    raise ValueError("Дерево не соответствует принятому реестру всего дома; неполный сбор не заменяет прежние данные.")
                store.set("inventory_expected_count", expected)
                status["total"] = len(rows)
                result, errors = [], []
                previous = {row["element_id"]: row for row in store.get("inventory", [])}
                for identity in rows:
                    if stop.wait(max(1, settings.request_interval)):
                        status["state"] = "stopped"
                        store.set("scan_status", status)
                        return
                    try:
                        data = client.read("element", id=identity["element_id"])
                        result.append(inventory_row(identity, data, now()))
                    except WaviotError as error:
                        old = previous.get(identity["element_id"], {**identity, "resources": [], "gas": []})
                        result.append({**old, "error": error.code})
                        errors.append(error.code)
                        if error.code in ("rate_limited", "api_error", "network_error", "timeout"):
                            raise
                    status["processed"] += 1
                    store.set("scan_status", status)
                store.set("inventory", result)
                refresh_case_snapshots(store)
                store.set("inventory_source", "Живой сбор API: " + now())
                status.update(state="incomplete" if errors else "complete", finished_at=now(), error=", ".join(sorted(set(errors))) if errors else "нет")
        except (WaviotError, ValueError, OSError, KeyError, TypeError) as error:
            status.update(state="failed", finished_at=now(), error=error.code if isinstance(error, WaviotError) else str(error)[:300])
        store.set("scan_status", status)
