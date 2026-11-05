"""Latest published meter readings for one apartment in the accepted register."""

import json
from datetime import datetime, timedelta, timezone
from html import escape

from .config import load_settings
from .access import Access
from .messages import chunks
from .storage import Store
from .waviot_client import WaviotClient, WaviotError, counter_value


ELECTRIC_TOTAL = "electro_ac_p_lsum_tsum"
READING_TIMEZONE = timezone(timedelta(hours=3))
UNITS = {"m³": "м³", "m3": "м³", "м³": "м³", "kWh": "кВт·ч"}


def _reading(register, now):
    try:
        value = counter_value(register.get("last_value"))
    except ValueError:
        value = None
    unit = str(register.get("unit_id") or "единица не указана")
    amount = "нет данных" if value is None else f"{str(value).replace('.', ',')} {UNITS.get(unit, unit)}"
    timestamp = register.get("last_value_timestamp")
    try:
        if timestamp is None or isinstance(timestamp, bool) or int(timestamp) <= 0:
            raise ValueError
        observed = datetime.fromtimestamp(int(timestamp), READING_TIMEZONE)
        date = observed.strftime("%d.%m.%Y %H:%M")
        if (now - observed).total_seconds() > 72 * 3600:
            date += " · ⚠ данные старше 72 часов"
        elif observed > now:
            date += " · ⚠ дата в будущем"
    except (ValueError, TypeError, OverflowError, OSError):
        date = "дата не указана"
    return escape(amount), date


def format_readings(identity, response, now):
    lines = [f"🏠 <b>Квартира {escape(str(identity['apartment']))}</b>", escape(str(identity['corpus']))]
    raw = response.get("devices", {})
    devices = list(raw.values()) if isinstance(raw, dict) else raw
    for resource, icon, title, missing in (("Gas", "🔥", "Газ", "Газ: не привязан"), ("Water", "💧", "Вода", "Вода: не привязана"), ("Electricity", "⚡", "Электричество", "Электричество: не привязано")):
        selected = [device for device in devices if device.get("class_name") == resource]
        if not selected:
            lines.extend(["", f"{icon} {missing} в API"])
            continue
        for device in selected:
            readings = []
            for key, register in device.get("registrators", {}).items():
                channel = str(register.get("channel_id") or key)
                if resource == "Gas" and (key == "gas" or channel == "1053"):
                    readings.append(("Показание", register))
                elif resource == "Water" and str(register.get("unit_id")) in ("m³", "m3", "м³") and "event" not in key:
                    label = "Горячая вода" if "hot" in key or "hot" in channel else "Холодная вода" if "cold" in key or "cold" in channel else "Показание воды"
                    readings.append((label, register))
                elif resource == "Electricity" and channel == ELECTRIC_TOTAL:
                    readings.append(("Показание", register))
            heading = readings[0][0] if resource == "Water" and len(readings) == 1 else title
            lines.extend(["", f"{icon} <b>{heading}</b>"])
            for index, (label, register) in enumerate(readings or [("Показание", {})]):
                amount, date = _reading(register, now)
                if len(readings) > 1:
                    lines.append(f"<i>{label}</i>")
                lines.append(f"<b>{amount}</b>")
                if index == 0:
                    lines.append(f"№ <code>{escape(str(device.get('device_sn') or 'не указан'))}</code>")
                lines.append(f"🕒 {date}")
    lines.extend(["", f"📥 Получено: {now.astimezone(READING_TIMEZONE).strftime('%d.%m.%Y %H:%M')}"])
    return "\n".join(lines)


def fetch_flat_request(database, job_id, *, now=None):
    with Store(database) as store:
        job = store.db.execute("SELECT payload FROM outbox WHERE id=? AND kind='flat_lookup' AND state IN ('pending','inflight')", (job_id,)).fetchone()
        if not job:
            return
        identity = json.loads(job["payload"])
        actor_id = identity.get("actor_id")
        if type(actor_id) is not int or identity.get("chat_id") != actor_id or not Access(store).employee(actor_id):
            store.job_state(job_id, "skipped", error="Доступ отозван или неверный адресат запроса")
            return
        formatted = False
        try:
            settings = load_settings()
            with WaviotClient(settings.api_key, timeout=settings.timeout) as client:
                data = client.read("element", id=identity["element_id"])
            text = format_readings(identity, data, now or datetime.now(timezone.utc))
            formatted = True
        except WaviotError as error:
            text = f"Кв. {identity['apartment']}: не удалось получить показания Waviot ({error.code}). Попробуйте позже. Отсутствие ответа не означает нулевое показание."
        except (ValueError, TypeError, KeyError, AttributeError, OSError):
            text = f"Кв. {identity['apartment']}: Waviot не вернул пригодные данные показаний. Попробуйте позже."
        with store.atomic():
            if not Access(store).employee(actor_id):
                store.job_state(job_id, "skipped", error="Доступ отозван во время запроса")
                return
            for index, part in enumerate(chunks(text)):
                payload = {"text": part, **{key: identity[key] for key in ("chat_id", "actor_id") if key in identity}}
                if formatted:
                    payload["parse_mode"] = "HTML"
                store.enqueue(f"flat-response:{job_id}:{index}", "message", payload)
            store.job_state(job_id, "complete")
