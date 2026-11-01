from access_helpers import employee
from datetime import datetime, timezone

from waviot_telegram.delivery import deliver
from waviot_telegram.storage import Store
from waviot_telegram.telegram_actions import BotActions


class API:
    def __init__(self):
        self.messages = []

    def call(self, method, **payload):
        self.messages.append((method, payload))
        return {"message_id": len(self.messages)}


def test_group_prompt_rejects_other_author_and_a_newer_case_version(tmp_path):
    now = datetime(2026, 10, 31, 10, tzinfo=timezone.utc)
    api = API()
    with Store(tmp_path / "state.db") as store:
        store.set("group_id", -100)
        employee(store)
        employee(store, 2, "Анна")
        item = store.open_incident("test", "Квартира", "no_data", "Тест", now=now)
        deliver(store, api, now)
        message_id = store.incident(item["id"])["message_id"]
        callback = {"update_id": 1, "callback_query": {"id": "cb", "from": {"id": 1, "first_name": "Иван"}, "message": {"chat": {"id": -100}, "message_id": message_id}, "data": f"case:{item['id']}:1:fault"}}
        actions = BotActions(store, api)
        store.ingest([callback])
        actions.process(now)
        deliver(store, api, now)
        prompt = store.db.execute("SELECT message_id FROM chat_replies").fetchone()[0]
        def reply(update_id, actor_id):
            return {"update_id": update_id, "message": {"chat": {"id": -100}, "from": {"id": actor_id, "first_name": "Анна"}, "text": "Неисправность", "reply_to_message": {"message_id": prompt}}}
        store.ingest([reply(2, 2)])
        actions.process(now)
        assert store.incident(item["id"])["diagnosis"] == "unknown"
        store.transition(item["id"], 1, "claim", 2, "Анна", now=now)
        store.ingest([reply(3, 1)])
        actions.process(now)
        current = store.incident(item["id"])
        assert current["assignee"] == "Анна" and current["diagnosis"] == "unknown"
