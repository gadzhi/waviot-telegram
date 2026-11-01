from access_helpers import employee
from datetime import datetime, timezone
from xml.etree import ElementTree

from waviot_telegram.delivery import deliver
from waviot_telegram.messages import card
from waviot_telegram.navigation import configure_navigation
from waviot_telegram.storage import Store
from waviot_telegram.telegram_actions import BotActions


NOW = datetime(2026, 10, 31, 13, tzinfo=timezone.utc)


class API:
    def __init__(self):
        self.calls = []

    def call(self, method, **payload):
        self.calls.append((method, payload))
        return {"message_id": len(self.calls)}


def test_case_card_has_safe_html_and_keeps_versioned_actions(tmp_path):
    with Store(tmp_path / "state.db") as store:
        item = store.open_incident("gas", "Корпус <1>, кв. 46", "no_data", "Дата <старая> & неизвестная причина", now=NOW)
        message = card(item)
    root = ElementTree.fromstring("<message>" + message["text"] + "</message>")
    visible = "".join(root.itertext())
    assert message["parse_mode"] == "HTML" and "📋" in visible and "👤" in visible
    assert "Корпус <1>, кв. 46" in visible and "<старая> &" in visible
    assert "Europe/Moscow" not in visible and "GMT" not in visible
    callbacks = [button["callback_data"] for row in message["reply_markup"]["inline_keyboard"] for button in row]
    assert f"case:{item['id']}:1:claim" in callbacks
    assert f"history:{item['id']}" in callbacks


def test_menu_buttons_route_like_commands_and_prompt_for_apartment(tmp_path):
    api = API()
    with Store(tmp_path / "state.db") as store:
        employee(store)
        store.ingest([{"update_id": i, "message": {"chat": {"id": 1, "type": "private"}, "from": {"id": 1}, "text": text}} for i, text in enumerate(["📊 Показания", "📋 Случаи", "❓ Помощь"], 1)])
        BotActions(store, api).process(NOW)
        deliver(store, api, NOW)
    replies = [payload for method, payload in api.calls if method == "sendMessage"]
    assert any("номер квартиры" in p["text"] for p in replies)
    assert any("рабочей группе" in p["text"] for p in replies)
    keyboard = next(p["reply_markup"] for p in replies if "keyboard" in p.get("reply_markup", {}))
    assert keyboard["resize_keyboard"] and keyboard["is_persistent"]
    assert any(button["text"] == "📊 Показания" for row in keyboard["keyboard"] for button in row)


def test_navigation_is_chat_scoped_and_does_not_repeat_startup_messages(tmp_path):
    api = API()
    with Store(tmp_path / "state.db") as store:
        assert not configure_navigation(store, api) and not api.calls
        employee(store)
        configure_navigation(store, api)
        configure_navigation(store, api)
        deliver(store, api, NOW)
    commands = [p for method,p in api.calls if method == "setMyCommands"]
    assert commands and all(p["scope"] == {"type": "chat", "chat_id": 1} for p in commands)
    assert any(command["command"] == "flat" for command in commands[0]["commands"])
    messages = [p for method,p in api.calls if method == "sendMessage"]
    assert not messages


def test_navigation_button_is_not_saved_as_a_fault_explanation(tmp_path):
    api = API()
    with Store(tmp_path / "state.db") as store:
        employee(store)
        item = store.open_incident("gas", "Квартира 1", "no_data", "Тест", now=NOW)
        store.enqueue("prompt", "prompt", {"text": "Укажите причину", "incident_id": item["id"], "version": 1, "action": "fault", "actor_id": 1, "chat_id": 1})
        deliver(store, api, NOW)
        prompt_id = store.db.execute("SELECT message_id FROM chat_replies").fetchone()[0]
        store.ingest([{"update_id": 1, "message": {"chat": {"id": 1, "type": "private"}, "from": {"id": 1}, "text": "📋 Случаи", "reply_to_message": {"message_id": prompt_id}}}])
        BotActions(store, api).process(NOW)
        assert store.incident(item["id"])["diagnosis"] == "unknown"


def test_long_escaped_case_fields_fit_telegram_without_breaking_html(tmp_path):
    with Store(tmp_path / "state.db") as store:
        item = store.open_incident("gas", "Квартира" * 200, "no_data", "<&😀" * 1000, now=NOW)
        item.update(reason="<&😀" * 2000, assignee="&😀" * 100, updated_by="&😀" * 100)
        result = card(item)
    root = ElementTree.fromstring("<message>" + result["text"] + "</message>")
    visible = "".join(root.itertext())
    assert len(visible.encode("utf-16-le")) // 2 < 4096
