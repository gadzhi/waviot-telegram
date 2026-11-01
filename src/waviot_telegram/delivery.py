"""Deliver persisted work; ambiguous sends require an explicit reconciliation."""

import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .messages import card, DIAGNOSES, display_field, CASE_REASONS
from .telegram_client import TelegramError
from .access import Access


def recover_delivery(store):
    with store.atomic():
        store.db.execute("UPDATE outbox SET state='pending' WHERE state='inflight' AND kind='flat_lookup'")
        store.db.execute("UPDATE outbox SET state='unknown',error='Процесс остановился до подтверждения отправки' WHERE state='inflight'")


def in_window(now, timezone):
    return 9 <= now.astimezone(ZoneInfo(timezone)).hour < 18


def next_window(now, timezone):
    local = now.astimezone(ZoneInfo(timezone))
    target = local.replace(hour=9, minute=0, second=0, microsecond=0)
    if local.hour >= 18:
        target += timedelta(days=1)
    return target.timestamp()


def deliver(store, api, now, timezone="Europe/Moscow"):
    for job in store.jobs(now.timestamp()):
        payload = json.loads(job["payload"])
        if job["kind"] in ("card", "digest", "reminder") and (payload.get("chat_id") != store.get("group_id") or payload.get("group_generation") != store.get("group_generation")):
            store.job_state(job["id"], "skipped", error="Отправка относится к прежнему подключению группы")
            continue
        chat_id = payload.get("chat_id") if job["kind"] not in ("card", "digest", "reminder") else store.get("group_id")
        if chat_id is None:
            store.job_state(job["id"], "skipped", error="Адресат не задан или рабочая группа ещё не подключена")
            continue
        if not payload.get("registration"):
            if job["kind"] not in ("card", "digest", "reminder") and not Access(store).employee(payload.get("actor_id")):
                store.job_state(job["id"], "skipped", error="Доступ сотрудника отозван")
                continue
            if chat_id < 0 and chat_id != store.get("group_id") and not payload.get("setup"):
                store.job_state(job["id"], "skipped", error="Группа отключена")
                continue
            if chat_id < 0 and not payload.get("setup") and payload.get("group_generation") != store.get("group_generation"):
                store.job_state(job["id"], "skipped", error="Группа была переподключена")
                continue
            if chat_id > 0 and chat_id != payload.get("actor_id"):
                store.job_state(job["id"], "skipped", error="Неверный адресат личного ответа")
                continue
        if payload.get("scheduled") and not in_window(now, timezone):
            store.job_state(job["id"], "pending", not_before=next_window(now, timezone))
            continue
        method, message_id = "sendMessage", None
        reminder_ids = []
        if job["kind"] == "card":
            item = store.incident(payload["incident_id"])
            if not item or item["version"] != payload["version"]:
                store.job_state(job["id"], "skipped")
                continue
            if any(json.loads(j["payload"]).get("incident_id") == item["id"] and json.loads(j["payload"]).get("group_generation") == store.get("group_generation") for j in store.uncertain_jobs() if j["kind"] == "card"):
                continue
            last = store.db.execute("SELECT actor FROM history WHERE incident_id=? ORDER BY id DESC LIMIT 1", (item["id"],)).fetchone()
            message = card({**item, "updated_by": last["actor"] if last else None})
            message_id = item["message_id"]
            if message_id:
                method = "editMessageText"
                message["message_id"] = message_id
        elif job["kind"] in ("digest", "reminder"):
            items = [store.incident(i) for i in payload["incident_ids"]]
            items = [i for i in items if i and i["state"] == "open" and (job["kind"] != "reminder" or not i["assignee"])]
            if not items:
                store.job_state(job["id"], "skipped")
                continue
            reminder_ids = [i["id"] for i in items if not i["assignee"]]
            heading = "⏰ Проверки без ответственного" if job["kind"] == "reminder" else "📋 Ежедневная сводка открытых проверок"
            text = f"<b>{heading}</b>\n" + "\n\n".join(f"<b>#{i['id']} · {'🧪 ' if i['demo'] else ''}{display_field(i['label'], 100)}</b>\n🚩 {CASE_REASONS.get(i['kind'], 'Основание требует уточнения')}\n👤 {display_field(i['assignee'] or 'не назначен', 60)}\n{DIAGNOSES[i['diagnosis']]}" for i in items)
            message = {"text": text, "parse_mode": "HTML"}
        else:
            message = {k: v for k, v in payload.items() if k in ("text", "reply_markup", "parse_mode")}
        store.job_state(job["id"], "inflight")
        try:
            result = api.call(method, chat_id=chat_id, **message)
        except TelegramError as error:
            if error.retry_after:
                store.job_state(job["id"], "pending", error=str(error), not_before=now.timestamp() + max(1, float(error.retry_after)))
                break
            if method == "editMessageText" and error.code == 400 and "message is not modified" in str(error).lower():
                result = {"message_id": message_id}
            elif method == "editMessageText" and error.code == 400 and "message to edit not found" in str(error).lower():
                with store.atomic():
                    store.set_incident_message_id(payload["incident_id"], None)
                    store.job_state(job["id"], "pending", error="Удалённая карточка будет создана заново")
                continue
            elif method == "editMessageText" and error.uncertain:
                store.job_state(job["id"], "pending", error=str(error), not_before=now.timestamp() + 15)
                break
            else:
                store.job_state(job["id"], "unknown" if error.uncertain else "failed", error=str(error))
                if error.code == 403 and chat_id == store.get("group_id"):
                    store.set("group_delivery_error", "Telegram запретил доставку в группу. Проверьте, что бот остаётся участником группы.")
                if error.code in (401, 409):
                    raise
                continue
        new_message_id = result.get("message_id", message_id) if isinstance(result, dict) else message_id
        with store.atomic():
            store.job_state(job["id"], "sent", message_id=new_message_id)
            if job["kind"] == "card":
                store.set_incident_message_id(payload["incident_id"], new_message_id)
            elif job["kind"] == "prompt":
                store.db.execute("INSERT OR REPLACE INTO chat_replies VALUES (?,?,?,?,?,?)", (chat_id, new_message_id, payload["incident_id"], payload["version"], payload["action"], payload["actor_id"]))
            for incident_id in reminder_ids:
                store.set(f"reminded:{incident_id}", now.isoformat())


def schedule(store, now, timezone="Europe/Moscow"):
    if not store.get("group_id") or not in_window(now, timezone):
        return
    day = now.astimezone(ZoneInfo(timezone)).date().isoformat()
    items = store.incidents()
    for job in store.db.execute("SELECT id,payload FROM outbox WHERE kind IN ('digest','reminder') AND state='pending'").fetchall():
        if json.loads(job["payload"]).get("day") != day:
            store.job_state(job["id"], "skipped")
    if store.get("digest_date") != day and items:
        for index in range(0, len(items), 12):
            store.enqueue(f"digest:{day}:{index//12}", "digest", {"incident_ids": [i["id"] for i in items[index:index+12]], "scheduled": True, "day": day})
        store.set("digest_date", day)
        return
    awaiting_digest = set()
    for row in store.db.execute("SELECT payload FROM outbox WHERE kind='digest' AND state IN ('pending','inflight','unknown')"):
        payload = json.loads(row["payload"])
        if payload.get("day") == day:
            awaiting_digest.update(payload["incident_ids"])
    for item in items:
        if item["id"] in awaiting_digest:
            continue
        if item["assignee"]:
            continue
        last = datetime.fromisoformat(store.get(f"reminded:{item['id']}", item["created_at"]))
        if (now - last).total_seconds() >= 86400:
            store.enqueue(f"reminder:{item['id']}:{day}", "reminder", {"incident_ids": [item["id"]], "scheduled": True, "day": day})
