from access_helpers import employee
from datetime import datetime, timezone

import pytest

from waviot_telegram.access import Access
from waviot_telegram.config import Settings
from waviot_telegram.delivery import deliver, recover_delivery
from waviot_telegram.flat_readings import fetch_flat_request, format_readings
from waviot_telegram.storage import Store
from waviot_telegram.telegram_actions import BotActions


NOW = datetime(2026, 10, 31, 13, tzinfo=timezone.utc)
IDENTITY = {"element_id": "101", "corpus": "Корпус 5", "apartment": "211"}
DATA = {"devices": {
    "1": {"class_name": "Gas", "device_sn": "TEST-GAS", "registrators": {
        "gas": {"channel_id": "1053", "unit_id": "m³", "last_value": "0.100", "modem_value": "1.0", "last_value_timestamp": 1736424782},
        "water_event_log": {"last_value": "99"}}},
    "2": {"class_name": "Water", "device_sn": "TEST-WATER", "registrators": {
        "cold": {"channel_id": "cold_water_flow_water7", "unit_id": "m³", "last_value": "0.000", "last_value_timestamp": 1793451600}}},
    "3": {"class_name": "Electricity", "device_sn": "TEST-ELECTRIC", "registrators": {
        "electro_ac_p_lsum_tsum": {"unit_id": "kWh", "last_value": "123.456", "last_value_timestamp": 1793451600},
        "electro_ac_p_lsum_t1": {"unit_id": "kWh", "last_value": "100.000", "last_value_timestamp": 1793451600}}}
}}


class Telegram:
    def __init__(self):
        self.sent = []

    def call(self, method, **payload):
        self.sent.append(payload)
        return {"message_id": len(self.sent)}


@pytest.mark.parametrize("command", ["211", "/flat 211"])
def test_number_lookup_fetches_registered_flat_and_replies_with_latest_readings(tmp_path, monkeypatch, command):
    database = tmp_path / "state.db"
    api = Telegram()

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read(self, endpoint, **params):
            assert endpoint == "element" and params == {"id": "101"}
            return DATA

    monkeypatch.setattr("waviot_telegram.flat_readings.WaviotClient", Client)
    monkeypatch.setattr("waviot_telegram.flat_readings.load_settings", lambda: Settings(api_key="synthetic"))
    with Store(database) as store:
        employee(store)
        store.set("inventory", [IDENTITY])
        store.ingest([{"update_id": 1, "message": {"chat": {"id": 1, "type": "private"}, "from": {"id": 1}, "text": command}}])
        BotActions(store, api).process(NOW)
        job_id = store.db.execute("SELECT id FROM outbox WHERE kind='flat_lookup'").fetchone()[0]
    fetch_flat_request(database, job_id, now=NOW)
    with Store(database) as store:
        deliver(store, api, NOW)
    answer = "\n".join(p["text"] for p in api.sent)
    assert "🏠 <b>Квартира 211</b>" in answer and "Корпус 5" in answer
    assert "0,100 м³" in answer and "0,000 м³" in answer
    assert "123,456 кВт·ч" in answer and "100,000 кВт·ч" not in answer
    assert "Тариф" not in answer and "T1" not in answer
    assert "Europe/Moscow" not in answer and "GMT" not in answer
    assert "не расход за период" not in answer
    assert all(p.get("parse_mode") == "HTML" for p in api.sent)
    assert "🔥" in answer and "💧" in answer and "⚡" in answer
    assert "09.01.2025 15:13" in answer and "старше 72" in answer
    assert "TEST-GAS" in answer and "99" not in answer


def test_missing_binding_and_unknown_value_do_not_become_zero():
    text = format_readings(IDENTITY, {"devices": {"1": {"class_name": "Gas", "registrators": {"gas": {"unit_id": "m³", "last_value": None, "last_value_timestamp": None}}}}}, NOW)
    assert "нет данных" in text
    assert "Вода: не привязана" in text and "Электричество: не привязано" in text
    assert "0 м³" not in text and "0,0" not in text


def test_reading_html_escapes_waviot_fields_and_always_uses_gmt_plus_three():
    from xml.etree import ElementTree
    identity = {**IDENTITY, "corpus": "Корпус <5> & секция", "apartment": "211"}
    text = format_readings(identity, {"devices": {"1": {"class_name": "Gas", "device_sn": "<b>SN&1</b>", "registrators": {"gas": {"last_value": "1.250", "unit_id": "m³", "last_value_timestamp": 1793451600}}}}}, NOW)
    root = ElementTree.fromstring("<message>" + text + "</message>")
    assert "Корпус <5> & секция" in "".join(root.itertext())
    assert "&lt;b&gt;SN&amp;1&lt;/b&gt;" in text
    assert "31.10.2026 16:00" in text and "Europe/Moscow" not in text


def test_no_electric_total_is_unknown_without_summing_tariffs():
    text = format_readings(IDENTITY, {"devices": {"1": {"class_name": "Electricity", "registrators": {"electro_ac_p_lsum_t1": {"last_value": "123.456", "unit_id": "kWh"}}}}}, NOW)
    assert "нет данных" in text and "123,456" not in text and "Тариф" not in text


def test_foreign_chat_and_unknown_number_cannot_queue_a_meter_request(tmp_path):
    api = Telegram()
    with Store(tmp_path / "state.db") as store:
        employee(store)
        store.set("inventory", [IDENTITY])
        store.ingest([
            {"update_id": 1, "message": {"chat": {"id": 2, "type": "private"}, "from": {"id": 2}, "text": "/flat 211"}},
            {"update_id": 2, "message": {"chat": {"id": 1, "type": "private"}, "from": {"id": 1}, "text": "/flat 999"}},
            {"update_id": 3, "message": {"chat": {"id": 1, "type": "private"}, "from": {"id": 1}, "text": "/flat"}},
        ])
        BotActions(store, api).process(NOW)
        deliver(store, api, NOW)
        assert store.db.execute("SELECT COUNT(*) FROM outbox WHERE kind='flat_lookup'").fetchone()[0] == 0
        assert all(message["chat_id"] == 1 for message in api.sent if "Для доступа" not in message["text"] and "Регистрация" not in message["text"])
        assert any("не найдена" in message["text"] for message in api.sent)
        assert any("/flat 211" in message["text"] for message in api.sent)


def test_read_only_lookup_resumes_after_restart_without_becoming_a_send(tmp_path):
    api = Telegram()
    with Store(tmp_path / "state.db") as store:
        employee(store)
        store.enqueue("lookup", "flat_lookup", IDENTITY)
        job_id = store.db.execute("SELECT id FROM outbox WHERE key='lookup'").fetchone()[0]
        store.job_state(job_id, "inflight")
        recover_delivery(store)
        deliver(store, api, NOW)
        assert not api.sent and not store.uncertain_jobs()
        assert store.db.execute("SELECT state FROM outbox WHERE id=?", (job_id,)).fetchone()[0] == "pending"


@pytest.mark.parametrize("change", ["disable", "rebind", "foreign_chat"])
def test_queued_lookup_checks_access_before_contacting_waviot(tmp_path, monkeypatch, change):
    database = tmp_path / "state.db"
    with Store(database) as store:
        employee(store)
        store.enqueue("lookup", "flat_lookup", {**IDENTITY, "actor_id": 1, "chat_id": 2 if change == "foreign_chat" else 1})
        job_id = store.db.execute("SELECT id FROM outbox WHERE key='lookup'").fetchone()[0]
        if change == "disable":
            Access(store).disable(1)
        elif change == "rebind":
            Access(store).invite(1, rebind=True, now=NOW)
    def unexpected_client(*args, **kwargs):
        pytest.fail("Запрос Waviot не должен начинаться без действующего доступа")
    monkeypatch.setattr("waviot_telegram.flat_readings.WaviotClient", unexpected_client)
    fetch_flat_request(database, job_id, now=NOW)
    with Store(database) as store:
        assert store.db.execute("SELECT state FROM outbox WHERE id=?", (job_id,)).fetchone()[0] == "skipped"
        assert not store.db.execute("SELECT 1 FROM outbox WHERE kind='message'").fetchone()


def test_access_revoked_during_lookup_discards_result(tmp_path, monkeypatch):
    database = tmp_path / "state.db"
    with Store(database) as store:
        employee(store)
        store.enqueue("lookup", "flat_lookup", {**IDENTITY, "actor_id": 1, "chat_id": 1})
        job_id = store.db.execute("SELECT id FROM outbox WHERE key='lookup'").fetchone()[0]
    class Client:
        def __init__(self, *args, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self, *args, **kwargs):
            with Store(database) as store:
                Access(store).disable(1)
            return DATA
    monkeypatch.setattr("waviot_telegram.flat_readings.WaviotClient", Client)
    monkeypatch.setattr("waviot_telegram.flat_readings.load_settings", lambda: Settings(api_key="synthetic"))
    fetch_flat_request(database, job_id, now=NOW)
    with Store(database) as store:
        assert store.db.execute("SELECT state FROM outbox WHERE id=?", (job_id,)).fetchone()[0] == "skipped"
        assert not store.db.execute("SELECT 1 FROM outbox WHERE kind='message'").fetchone()
