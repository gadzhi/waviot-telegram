from access_helpers import employee
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from waviot_telegram.delivery import deliver, recover_delivery, schedule
from waviot_telegram.storage import Store
from waviot_telegram.telegram_client import TelegramError


class API:
    def __init__(self, error=None):
        self.error = error
        self.sent = []

    def call(self, method, **payload):
        if self.error:
            raise self.error
        self.sent.append(payload)
        return {"message_id": len(self.sent)}


def test_daily_digest_waits_for_house_morning_and_survives_restart(tmp_path):
    path = tmp_path / "state.db"
    now = datetime(2026, 10, 31, 8, 59, tzinfo=ZoneInfo("Europe/Moscow"))
    api = API()
    with Store(path) as store:
        store.set("group_id", -100)
        employee(store)
        store.open_incident("gas", "Квартира 1", "no_data", "Тест", now=now)
        deliver(store, api, now)  # Interactive card is allowed at any time.
        api.sent.clear()
        schedule(store, now)
        deliver(store, api, now)
        assert not api.sent
    with Store(path) as store:
        morning = now + timedelta(minutes=1)
        schedule(store, morning)
        deliver(store, api, morning)
        assert len(api.sent) == 1 and "сводка" in api.sent[0]["text"]
        schedule(store, morning + timedelta(minutes=1))
        deliver(store, api, morning + timedelta(minutes=1))
        assert len(api.sent) == 1


def test_uncertain_send_is_not_repeated_but_rate_limit_can_retry(tmp_path):
    now = datetime(2026, 10, 31, 10, tzinfo=timezone.utc)
    with Store(tmp_path / "state.db") as store:
        store.set("group_id", -100)
        employee(store)
        store.enqueue("first", "message", {"text": "Первое", "chat_id": 1, "actor_id": 1})
        deliver(store, API(TelegramError("timeout", uncertain=True)), now)
        api = API()
        recover_delivery(store)
        deliver(store, api, now + timedelta(minutes=1))
        assert not api.sent and len(store.uncertain_jobs()) == 1
        store.enqueue("second", "message", {"text": "Второе", "chat_id": 1, "actor_id": 1})
        deliver(store, API(TelegramError(429, retry_after=12)), now)
        deliver(store, api, now + timedelta(seconds=11))
        assert not api.sent
        deliver(store, api, now + timedelta(seconds=12))
        assert len(api.sent) == 1 and api.sent[0]["text"] == "Второе"
