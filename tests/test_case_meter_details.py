from datetime import datetime, timezone
import sqlite3
from xml.etree import ElementTree

from waviot_telegram.collector import inventory_row, stale_candidates
from waviot_telegram.messages import card
from waviot_telegram.storage import Store


NOW = datetime(2026, 10, 31, 13, tzinfo=timezone.utc)
SNAPSHOT = {"apartment": "46", "corpus": "Корпус 1", "meter_sn": "TEST-123", "last_value": "12.340", "unit": "m³", "last_timestamp": 1736424782, "observed_at": NOW.isoformat()}


def visible(message):
    return "".join(ElementTree.fromstring("<message>" + message["text"] + "</message>").itertext())


def test_case_meter_details_survive_restart_and_have_explicit_signal(tmp_path):
    path = tmp_path / "state.db"
    with Store(path) as store:
        item = store.open_incident("gas", "Корпус 1, кв. 46", "no_data", "Старая метка", meter_snapshot=SNAPSHOT, now=NOW)
    with Store(path) as store:
        item = store.incident(item["id"])
        text = visible(card(item))
        assert item["meter_snapshot"] == SNAPSHOT
        assert "Квартира 46" in text and "TEST-123" in text
        assert "12,340 м³" in text and "09.01.2025 15:13" in text
        assert "Данные счётчика не поступают" in text
        assert "Причина не установлена" in text
        flat = store.open_incident("gas-2", "Квартира 47", "gas_flat_with_activity", "Три суток", meter_snapshot={**SNAPSHOT, "apartment": "47", "last_value": "0.000"}, now=NOW)
        text = visible(card(flat))
        assert "Нет расхода" in text and "0,000 м³" in text
        assert "Данные счётчика не поступают" not in text


def test_candidate_carries_factory_number_and_published_last_value():
    row = inventory_row({"element_id": "1", "corpus": "Корпус 1", "apartment": "46"}, {"devices": {"gas": {
        "id": 10, "class_name": "Gas", "modem_id": "abc", "device_sn": "TEST-123", "registrators": {"gas": {
            "channel_id": "1053", "last_value": "0.000", "unit_id": "m³", "last_value_timestamp": 1736424782
        }}
    }}}, NOW.isoformat())
    candidate = stale_candidates([row], NOW)[0]
    assert candidate["meter_snapshot"]["meter_sn"] == "TEST-123"
    assert candidate["meter_snapshot"]["last_value"] == "0.000"
    assert candidate["meter_snapshot"]["apartment"] == "46"


def test_existing_database_gets_new_details_column_without_losing_case_history(tmp_path):
    path = tmp_path / "state.db"
    with Store(path) as store:
        item = store.open_incident("gas", "Квартира 46", "no_data", "Старый случай", now=NOW)
        store.transition(item["id"], 1, "claim", 1, "Иван", now=NOW)
    with sqlite3.connect(path) as db:
        db.execute("ALTER TABLE incidents DROP COLUMN meter_snapshot")
    with Store(path) as store:
        restored = store.incident(item["id"])
        assert restored["assignee"] == "Иван" and restored["version"] == 2
        assert restored["meter_snapshot"] == {} and len(store.history(item["id"])) == 2


def test_refreshing_readings_keeps_human_result_version_and_closed_snapshot(tmp_path):
    with Store(tmp_path / "state.db") as store:
        item = store.open_incident("gas", "Квартира 46", "no_data", "Старая метка", meter_snapshot=SNAPSHOT, now=NOW)
        store.transition(item["id"], 1, "fault", 1, "Иван", "Осмотрен прибор", now=NOW)
        latest = {**SNAPSHOT, "last_value": "12.500"}
        store.update_meter_snapshot(item["id"], latest)
        updated = store.incident(item["id"])
        assert updated["meter_snapshot"] == latest
        assert updated["version"] == 2 and updated["diagnosis"] == "fault" and updated["kind"] == "no_data"
        assert len(store.history(item["id"])) == 2
        store.transition(item["id"], 2, "repaired", 1, "Иван", "Исправлен", now=NOW)
        store.update_meter_snapshot(item["id"], SNAPSHOT)
        assert store.incident(item["id"])["meter_snapshot"] == latest


def test_unknown_reading_and_unknown_signal_are_explicit(tmp_path):
    with Store(tmp_path / "state.db") as store:
        item = store.open_incident("gas", "Квартира 46", "unclassified", "Нужно уточнить", meter_snapshot={**SNAPSHOT, "last_value": None, "last_timestamp": None}, now=NOW)
    text = visible(card(item))
    assert "Последнее показание: нет данных" in text and "Дата показания: нет данных" in text
    assert "Основание требует уточнения" in text
    assert "Нет расхода" not in text and "Данные счётчика не поступают" not in text


def test_meter_details_keep_long_card_safe_and_within_telegram_limit(tmp_path):
    long_field = "<&😀" * 2000
    snapshot = {**SNAPSHOT, **{key: long_field for key in ("apartment", "corpus", "meter_sn", "last_value", "unit")}}
    with Store(tmp_path / "state.db") as store:
        item = store.open_incident("gas", "Квартира 46", "no_data", long_field, meter_snapshot=snapshot, now=NOW)
        item.update(reason=long_field, assignee=long_field, updated_by=long_field)
    text = visible(card(item))
    assert len(text.encode("utf-16-le")) // 2 < 4096
    assert "Счётчик №" in text and "Последнее показание" in text
