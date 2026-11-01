from access_helpers import employee
from datetime import datetime, timezone

from waviot_telegram.storage import Store
from waviot_telegram.telegram_actions import BotActions
from waviot_telegram.delivery import deliver


class Telegram:
    def __init__(self):
        self.calls = []

    def call(self, method, **payload):
        self.calls.append((method, payload))
        return {"message_id": len(self.calls)} if method in ("sendMessage", "editMessageText") else True


def update(i, text, chat=-100, reply=None):
    message = {"message_id": i, "chat": {"id": chat, "type": "supergroup"}, "from": {"id": 100 if chat == -100 else chat, "first_name": "Иван"}, "text": text}
    if reply:
        message["reply_to_message"] = {"message_id": reply}
    return {"update_id": i, "message": message}


def test_pairing_and_replayed_demo_command_send_one_card(tmp_path):
    now = datetime(2026, 10, 31, 10, tzinfo=timezone.utc)
    api = Telegram()
    with Store(tmp_path / "state.db") as store:
        store.set("pairing_code", "one-time-code")
        actions = BotActions(store, api)
        store.ingest([update(1, "/start wrong", chat=200)])
        actions.process(now)
        assert store.get("chat_id") is None
        assert not store.incidents()
        store.ingest([update(2, "/start one-time-code")])
        actions.process(now)
        assert store.get("chat_id") is None
        store.set("group_id", -100)
        employee(store, 100)
        store.ingest([update(3, "/demo"), update(4, "/demo", chat=200)])
        actions.process(now)
        store.ingest([update(3, "/demo")])
        actions.process(now)
        deliver(store, api, now)
        incidents = store.incidents()
        assert len(incidents) == 1 and incidents[0]["demo"]
        cards = [p for method,p in api.calls if method == "sendMessage" and "ТЕСТОВЫЙ" in p.get("text", "")]
        assert len(cards) == 1 and cards[0]["chat_id"] == -100
        assert incidents[0]["message_id"]


def test_reason_is_bound_to_prompt_actor_and_card_version(tmp_path):
    now = datetime(2026, 10, 31, 10, tzinfo=timezone.utc)
    api = Telegram()
    with Store(tmp_path / "state.db") as store:
        store.set("group_id", -100)
        employee(store, 100)
        item = store.open_incident("demo", "Тестовая квартира", "no_data", "Тест", demo=True, now=now)
        deliver(store, api, now)
        message_id = store.incident(item["id"])["message_id"]
        callback = {"update_id": 1, "callback_query": {"id": "cb", "from": {"id": 100, "first_name": "Иван"}, "message": {"chat": {"id": -100, "type": "supergroup"}, "message_id": message_id}, "data": f"case:{item['id']}:1:fault"}}
        actions = BotActions(store, api)
        store.ingest([callback])
        actions.process(now)
        deliver(store, api, now)
        prompt_id = store.db.execute("SELECT message_id FROM chat_replies").fetchone()[0]
        store.ingest([update(2, "Проверена батарея", chat=200, reply=prompt_id)])
        actions.process(now)
        assert store.incident(item["id"])["diagnosis"] == "unknown"
        store.ingest([update(3, "Проверена батарея", reply=prompt_id)])
        actions.process(now)
        deliver(store, api, now)
        assert store.incident(item["id"])["diagnosis"] == "fault"
        assert store.incident(item["id"])["state"] == "open"
        assert any(m == "editMessageText" and "Проверена батарея" in p["text"] for m,p in api.calls)
