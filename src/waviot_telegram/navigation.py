BUTTON_COMMANDS = {
    "📊 Показания": "/flat",
    "📋 Случаи": "/cases",
    "🔎 Кандидаты": "/candidates",
    "🏠 Дом": "/house",
    "🔄 Обновить": "/scan",
    "⚙️ Статус": "/status",
    "❓ Помощь": "/help",
}

COMMANDS = [
    {"command": "flat", "description": "Показания квартиры"},
    {"command": "cases", "description": "Открытые случаи"},
    {"command": "candidates", "description": "Кандидаты на проверку"},
    {"command": "house", "description": "Приборы дома"},
    {"command": "status", "description": "Состояние бота"},
    {"command": "scan", "description": "Обновить данные дома"},
    {"command": "help", "description": "Все команды"},
]


def keyboard():
    labels = list(BUTTON_COMMANDS)
    return {"keyboard": [[{"text": label} for label in labels[i:i+2]] for i in range(0, len(labels), 2)],
            "resize_keyboard": True, "is_persistent": True, "input_field_placeholder": "Номер квартиры или команда"}


def configure_navigation(store, api):
    from .access import Access
    targets = [e["telegram_id"] for e in Access(store).employees() if e["active"] and e["telegram_id"]]
    if store.get("group_id") is not None:
        targets.append(store.get("group_id"))
    for chat_id in targets:
        key = f"navigation:employees:1:{chat_id}"
        if store.get(key):
            continue
        from .telegram_client import TelegramError
        try:
            api.call("setMyCommands", commands=COMMANDS, scope={"type": "chat", "chat_id": chat_id})
            if chat_id > 0:
                api.call("setChatMenuButton", chat_id=chat_id, menu_button={"type": "commands"})
        except TelegramError as error:
            if error.code not in (400, 403):
                raise
            store.set(key, "unavailable")
            continue
        store.set(key, True)
    return bool(targets)
