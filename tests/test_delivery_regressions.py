from access_helpers import employee
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from waviot_telegram.delivery import deliver, schedule
from waviot_telegram.storage import Store
from waviot_telegram.telegram_actions import BotActions
from waviot_telegram.telegram_client import TelegramError


class API:
    def __init__(self):
        self.sent = []

    def call(self, method, **payload):
        self.sent.append(payload)
        return {"message_id": len(self.sent)}


def test_digest_at_night_is_coalesced_with_next_morning(tmp_path):
    evening = datetime(2026, 10, 31, 18, tzinfo=ZoneInfo("Europe/Moscow"))
    with Store(tmp_path / "state.db") as store:
        store.set("group_id", -100)
        employee(store)
        store.open_incident("gas", "Квартира", "no_data", "Тест", now=evening - timedelta(days=2))
        api = API()
        deliver(store, api, evening)
        api.sent.clear()
        schedule(store, evening)
        deliver(store, api, evening)
        morning = evening + timedelta(hours=15)
        schedule(store, morning)
        deliver(store, api, morning)
        assert len(api.sent) == 1


def test_reconciled_prompt_binds_human_reason(tmp_path):
    now = datetime(2026, 10, 31, 12, tzinfo=ZoneInfo("Europe/Moscow"))
    with Store(tmp_path / "state.db") as store:
        store.set("group_id", -100)
        employee(store)
        item = store.open_incident("gas", "Квартира", "no_data", "Тест", now=now)
        store.enqueue("prompt-uncertain", "prompt", {"incident_id": item["id"], "version": 1, "action": "fault", "actor_id": 1, "chat_id": -100, "text": "Причина"})
        job_id = store.db.execute("SELECT id FROM outbox WHERE key='prompt-uncertain'").fetchone()[0]
        store.job_state(job_id, "unknown")
        def message(update_id, text, reply=None):
            msg = {"chat": {"id": -100, "type": "supergroup"}, "from": {"id": 1, "first_name": "Иван"}, "text": text}
            if reply:
                msg["reply_to_message"] = {"message_id": reply}
            return {"update_id": update_id, "message": msg}
        store.ingest([message(1, f"/resolve {job_id} sent 123"), message(2, "Проверена батарея", reply=123)])
        BotActions(store, API()).process(now)
        assert store.incident(item["id"])["diagnosis"] == "fault"


def test_pending_rate_limited_digest_suppresses_sibling_reminder(tmp_path):
    now = datetime(2026, 10, 31, 9, tzinfo=ZoneInfo("Europe/Moscow"))
    class RateLimited(API):
        def call(self, *args, **kwargs):
            raise TelegramError(429, retry_after=120)
    with Store(tmp_path / "state.db") as store:
        store.set("group_id", -100)
        employee(store)
        store.open_incident("gas", "Квартира", "no_data", "Тест", now=now-timedelta(days=2))
        deliver(store, API(), now)
        schedule(store, now)
        deliver(store, RateLimited(), now)
        api = API()
        schedule(store, now+timedelta(seconds=10))
        deliver(store, api, now+timedelta(seconds=10))
        assert not api.sent
        schedule(store, now+timedelta(seconds=120))
        deliver(store, api, now+timedelta(seconds=120))
        assert len(api.sent) == 1
