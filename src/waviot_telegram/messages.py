from datetime import datetime, timedelta, timezone
from html import escape
import re


HELP = """Waviot · дом

/status — состояние бота и сбора
/house — покрытие всего дома
/flat 211 — последние показания квартиры (можно отправить просто 211)
/candidates — старые метки газовых данных для ручной проверки
/cases — открытые проверки
/history ID — история проверки
/scan — обновить метаданные всего дома
/demo — создать явно обозначенный тестовый случай
/delivery — отправки с неопределённым результатом
/help — команды

Частые команды также доступны кнопками под полем ввода и в меню Telegram.

Автоматические тревоги выключены: получение готового потребления газа и воды пока не подтверждено. Отсутствующие данные не считаются нулевым расходом. Кандидаты — наблюдения API, причина не установлена."""

DIAGNOSES = {"unknown": "Причина не установлена", "fault": "Неисправность подтверждена сотрудником", "normal": "Нормальная ситуация", "repaired": "Неисправность устранена"}
ACTIONS = {"created": "Создан", "claim": "Взят в работу / сменён ответственный", "fault": "Неисправность подтверждена", "unknown": "Причина не установлена", "normal": "Закрыт: нормальная ситуация", "repaired": "Закрыт: неисправность устранена", "recovered": "Данные восстановились"}
CASE_REASONS = {"no_data": "Данные счётчика не поступают", "gas_flat_with_activity": "Нет расхода"}


def local_time(value):
    return datetime.fromisoformat(value).astimezone(timezone(timedelta(hours=3))).strftime("%d.%m.%Y %H:%M")


def display_field(value, limit):
    text = str(value)
    raw = text.encode("utf-16-le")
    if len(raw) > limit * 2:
        text = raw[:(limit - 1) * 2].decode("utf-16-le", errors="ignore") + "…"
    return escape(text)


def evidence_text(value):
    def convert(match):
        try:
            return local_time(match.group())
        except ValueError:
            return match.group()
    return re.sub(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})", convert, value.replace(" (Europe/Moscow)", ""))


def card(item):
    heading = "🧪 ТЕСТОВЫЙ СЛУЧАЙ" if item["demo"] else "📋 Случай"
    status = "✅ Закрыт" if item["state"] == "closed" else "🔵 В работе" if item["assignee"] else "🟠 Открыт · ожидает назначения"
    evidence = evidence_text(item["evidence"])
    snapshot = item.get("meter_snapshot") or {}
    text = f"<b>{heading} #{item['id']}</b>\n"
    if snapshot.get("apartment"):
        text += f"🏠 <b>Квартира {display_field(snapshot['apartment'], 60)}</b>\n{display_field(snapshot.get('corpus') or '', 100)}\n"
    else:
        text += f"🏠 <b>{display_field(item['label'], 200)}</b>\n"
    text += f"\n🚩 <b>Причина обращения:</b> {CASE_REASONS.get(item['kind'], 'Основание требует уточнения')}\n"
    text += f"🔢 <b>Счётчик №</b> <code>{display_field(snapshot.get('meter_sn') or 'не указан', 100)}</code>\n"
    value = snapshot.get("last_value")
    unit = {"m³": "м³", "m3": "м³", "kWh": "кВт·ч"}.get(snapshot.get("unit"), snapshot.get("unit") or "ед. не указана")
    reading = f"{str(value).replace('.', ',')} {unit}" if value is not None else "нет данных"
    text += f"📟 <b>Последнее показание:</b> {display_field(reading, 100)}\n"
    try:
        timestamp = snapshot.get("last_timestamp")
        if timestamp is None or isinstance(timestamp, bool) or int(timestamp) <= 0:
            raise ValueError
        date = local_time(datetime.fromtimestamp(int(timestamp), timezone.utc).isoformat())
    except (TypeError, ValueError, OverflowError, OSError):
        date = "нет данных"
    text += f"🕒 Дата показания: {date}\n\n"
    text += f"<b>{status}</b>\n👤 Ответственный: <b>{display_field(item['assignee'] or 'не назначен', 100)}</b>\n\n"
    text += f"📌 <b>Основание</b>\n{display_field(evidence, 900)}\n\n"
    text += f"🔎 <b>Результат проверки</b>\n{DIAGNOSES[item['diagnosis']]}\n"
    if item["reason"]:
        text += f"\n💬 <b>Пояснение</b>\n{display_field(item['reason'], 1600)}\n"
    if item["recovered"]:
        text += "\n📶 Данные восстановились. Требуется решение сотрудника.\n"
    if item.get("updated_by"):
        text += f"\n✍️ {display_field(item['updated_by'], 100)}"
    text += f"\n🕒 {local_time(item['updated_at'])}"
    keyboard = []
    if item["state"] == "open":
        def button(label, action):
            return {"text": label, "callback_data": f"case:{item['id']}:{item['version']}:{action}"}
        keyboard = [[button("🙋 Взять в работу" if not item["assignee"] else "🙋 Назначить себя", "claim")],
                    [button("🔧 Неисправность", "fault"), button("❔ Причина неизвестна", "unknown")],
                    [button("✅ Нормальная ситуация · закрыть", "normal")]]
        if item["diagnosis"] == "fault":
            keyboard.append([button("🛠 Устранено · закрыть", "repaired")])
    keyboard.append([{"text": "📜 История", "callback_data": f"history:{item['id']}"}])
    return {"text": text, "parse_mode": "HTML", "reply_markup": {"inline_keyboard": keyboard}}


def text_parts(text, limit=2800):
    part, units = [], 0
    for character in text:
        width = 2 if ord(character) > 0xFFFF else 1
        if units + width > limit:
            yield "".join(part)
            part, units = [], 0
        part.append(character)
        units += width
    if part:
        yield "".join(part)


def chunks(text, limit=3500):
    while text:
        end = min(len(text), limit)
        if end < len(text):
            end = text.rfind("\n", 0, end) or end
            if end < 1:
                end = limit
        yield text[:end]
        text = text[end:].lstrip("\n")
