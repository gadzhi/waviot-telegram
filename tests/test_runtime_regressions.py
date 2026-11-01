from datetime import datetime, timezone

from waviot_telegram.storage import Store
from waviot_telegram.telegram_actions import BotActions


class API:
    def call(self, *args, **kwargs):
        return True


def test_unpaired_non_ascii_start_does_not_poison_persistent_inbox(tmp_path):
    with Store(tmp_path / "state.db") as store:
        store.set("pairing_code", "valid-code")
        def update(i, text):
            return {"update_id": i, "message": {"chat": {"id": 1, "type": "private"}, "from": {"id": 1}, "text": text}}
        store.ingest([update(1, "/start привет"), update(2, "/start valid-code")])
        BotActions(store, API()).process(datetime(2026, 10, 31, 10, tzinfo=timezone.utc))
        assert store.get("chat_id") is None and not store.pending_updates()


def test_empty_start_payload_does_not_block_later_pairing(tmp_path):
    with Store(tmp_path / "state.db") as store:
        store.set("pairing_code", "valid-code")
        store.ingest([{"update_id": 1, "message": {"chat": {"id": 1, "type": "private"}, "from": {"id": 1}, "text": "/start "}}])
        BotActions(store, API()).process(datetime(2026, 10, 31, 10, tzinfo=timezone.utc))
        assert store.get("chat_id") is None and not store.pending_updates()
