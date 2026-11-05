import re
from datetime import datetime, timezone
from html import escape

from .messages import ACTIONS, HELP, chunks, local_time, display_field, text_parts, evidence_text, CASE_REASONS
from .navigation import BUTTON_COMMANDS, keyboard
from .access import Access
from .config import load_settings
from .telegram_client import TelegramError


class InvalidNumber(ValueError):
    pass


def command_number(value, *, minimum=1):
    # Check length before int(): Python also limits conversion of huge strings.
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{1,19}", value):
        raise InvalidNumber
    number = int(value)
    if not minimum <= number <= 2**63 - 1:
        raise InvalidNumber
    return number


class BotActions:
    def __init__(self, store, api, *, scan=None, timezone="Europe/Moscow"):
        self.store = store
        self.api = api
        self.scan = scan
        self.timezone = timezone
        self.chat_id = None
        self.actor_id = None
        self.registration = False
        self.setup = False

    def reply(self, update_id, text, *, markup=None, formatted=False):
        for index, part in enumerate([text] if formatted else chunks(text)):
            payload = {"text": part, "chat_id": self.chat_id, "actor_id": self.actor_id, "registration": self.registration, "setup": self.setup}
            if formatted:
                payload["parse_mode"] = "HTML"
            if index == 0:
                payload["reply_markup"] = markup if markup is not None else keyboard()
            self.store.enqueue(f"response:{update_id}:{index}", "message", payload)

    def process(self, now=None):
        now = now or datetime.now(timezone.utc)
        for update_id, update in self.store.pending_updates():
            callback = update.get("callback_query")
            candidate = update.get("message", {})
            candidate_actor = candidate.get("from", {}).get("id")
            candidate_chat = candidate.get("chat", {})
            group_admin = False
            if candidate_chat.get("type") in ("group", "supergroup") and str(candidate.get("text", "")).split(" ")[0].split("@")[0] == "/connect" and Access(self.store).employee(candidate_actor) and not Access(self.store).limited(f"connect:{candidate_actor}", now):
                # Network I/O must not hold the SQLite write lock used by the admin.
                try:
                    members = self.api.call("getChatAdministrators", chat_id=candidate_chat["id"])
                    group_admin = any(member.get("user", {}).get("id") == candidate_actor and member.get("status") in ("creator", "administrator") for member in members)
                except TelegramError:
                    pass
            if isinstance(callback, dict) and callback.get("id"):
                try:
                    self.api.call("answerCallbackQuery", callback_query_id=callback["id"])
                except TelegramError:
                    pass
            with self.store.atomic():
                message = (callback or {}).get("message") if callback else update.get("message")
                if isinstance(message, dict):
                    chat = message.get("chat", {})
                    chat_id = chat.get("id")
                    actor = (callback or {}).get("from", {}) if callback else message.get("from", {})
                    actor_id = actor.get("id")
                    self.chat_id, self.actor_id = chat_id, actor_id
                    access = Access(self.store)
                    employee = access.employee(actor_id) if type(actor_id) is int else None
                    private = chat.get("type") == "private"
                    self.registration = private and not employee
                    self.setup = False
                    text = message.get("text", "")
                    if private and not employee and not callback:
                        self._registration(update_id, message, now)
                    elif employee:
                        if not callback and chat.get("type") in ("group", "supergroup") and str(text).split(" ")[0].split("@")[0] == "/connect":
                            self.setup = True
                            code = message.get("registration_code_hash") or str(text).partition(" ")[2].strip()
                            connected = group_admin and access.connect_group(chat_id, code, actor_id, now, hashed=bool(message.get("registration_code_hash")))
                            self.reply(update_id, "Рабочая группа подключена." if connected else "Не удалось подключить группу. Проверьте код, срок действия и права администратора.")
                        elif private or chat_id == self.store.get("group_id"):
                            try:
                                if callback:
                                    if private:
                                        self.reply(update_id, "Работа со случаями доступна в рабочей группе.")
                                    else:
                                        self._callback(update_id, callback, now)
                                else:
                                    self._message(update_id, message, now)
                            except InvalidNumber:
                                self.reply(update_id, "Некорректный номер. Используйте номер из актуальной карточки или списка.")
                    if private and not employee:
                        self.store.db.execute("UPDATE updates SET payload=? WHERE id=?", ('{"redacted":"registration"}', update_id))
                self.store.db.execute("UPDATE updates SET done=1 WHERE id=?", (update_id,))

    def _registration(self, update_id, message, now):
        access = Access(self.store)
        actor_id = message.get("from", {}).get("id")
        if type(actor_id) is not int or message.get("chat", {}).get("id") != actor_id:
            return
        contact = message.get("contact")
        if isinstance(contact, dict):
            ok = access.remember_contact(actor_id, contact, now)
            self.reply(update_id, "Номер получен. Введите одноразовый код от администратора." if ok else "Нажмите кнопку и отправьте собственный контакт Telegram.", markup={"remove_keyboard": True} if ok else self._contact_keyboard())
        elif message.get("registration_code_hash"):
            ok = access.register(actor_id, message["registration_code_hash"], now, hashed=True)
            self.reply(update_id, "✅ Регистрация завершена. Для показаний отправьте номер квартиры. Работа со случаями — в рабочей группе." if ok else "Регистрация не выполнена. Проверьте контакт и код у администратора. После пяти попыток подождите 15 минут.", markup=keyboard() if ok else self._contact_keyboard())
        else:
            self.reply(update_id, "Для доступа нужно приглашение администратора. Сначала поделитесь своим номером телефона кнопкой ниже.", markup=self._contact_keyboard())

    @staticmethod
    def _contact_keyboard():
        return {"keyboard": [[{"text": "📱 Поделиться номером", "request_contact": True}]], "resize_keyboard": True, "one_time_keyboard": True}

    def _message(self, update_id, message, now):
        text = message.get("text", "")
        if not isinstance(text, str) or not text.strip():
            return
        actor = message["from"]
        private = message.get("chat", {}).get("type") == "private"
        parsed = (BUTTON_COMMANDS.get(text.strip()) or text).split()[0].split("@")[0].lower()
        case_commands = {"/cases", "/candidates", "/demo", "/history", "/delivery", "/resolve"}
        if private and parsed in case_commands:
            self.reply(update_id, "Работа со случаями доступна в подключённой рабочей группе.")
            return
        button_command = BUTTON_COMMANDS.get(text.strip())
        reply_to = message.get("reply_to_message", {}).get("message_id")
        if reply_to and text and not text.startswith("/") and not button_command:
            pending = self.store.db.execute("SELECT * FROM chat_replies WHERE chat_id=? AND message_id=? AND actor_id=?", (self.chat_id, reply_to, actor["id"])).fetchone()
            if pending:
                _, changed = self.store.transition(pending["incident_id"], pending["version"], pending["action"], actor["id"], self._name(actor), text, now=now)
                self.store.db.execute("DELETE FROM chat_replies WHERE chat_id=? AND message_id=?", (self.chat_id, reply_to))
                self.reply(update_id, "Результат сохранён, карточка обновлена." if changed else "Карточка уже изменилась. Откройте актуальную проверку через /cases и повторите действие.")
                return
        if not private and (parsed == "/flat" or re.fullmatch(r"[0-9]{1,4}", text.strip())):
            self.reply(update_id, "Для показаний отправьте номер квартиры в личный чат с ботом.")
            return
        command, *arguments = (button_command or text).split(maxsplit=1)
        command = command.split("@")[0].lower()
        argument = arguments[0].strip() if arguments else ""
        if command in ("/start", "/help", "/menu"):
            self.reply(update_id, HELP)
        elif command == "/flat" or re.fullmatch(r"[0-9]{1,4}", text.strip()):
            number = argument if command == "/flat" else text.strip()
            if not re.fullmatch(r"[0-9]{1,4}", number):
                self.reply(update_id, "Укажите номер квартиры: /flat 211. Можно просто отправить число 211.")
                return
            matches = [row for row in self.store.get("inventory", []) if str(row.get("apartment", "")).lstrip("0") == number.lstrip("0")]
            if len(matches) == 1:
                identity = {key: matches[0][key] for key in ("element_id", "corpus", "apartment")}
                identity.update(chat_id=self.chat_id, actor_id=self.actor_id)
                self.store.enqueue(f"flat-request:{update_id}", "flat_lookup", identity)
            elif not matches:
                self.reply(update_id, f"Квартира {number} не найдена в реестре дома. Проверьте номер. /house — состояние реестра.")
            else:
                self.reply(update_id, "Номер квартиры неоднозначен в реестре. Нужна проверка привязки корпуса.")
        elif command == "/status":
            scan = self.store.get("scan_status", {})
            uncertain = len(self.store.uncertain_jobs())
            self.reply(update_id, f"Бот работает локально. Часовой пояс дома: {self.timezone}.\nАвтоматические тревоги: выключены.\nПотребление газа/воды: недостаточно подтверждённых данных API.\nОткрытых проверок: {len(self.store.incidents())}\nНеопределённых отправок: {uncertain}\nСбор: {scan.get('state', 'ещё не запускался')}\nОбработано: {scan.get('processed', 0)} / {scan.get('total', 0)}\nПоследнее завершение: {scan.get('finished_at', 'нет')}\nОшибка: {scan.get('error', 'нет')}")
        elif command == "/house":
            inventory = self.store.get("inventory", [])
            counts = {resource: sum(resource in row.get("resources", []) for row in inventory) for resource in ("Gas", "Electricity", "Water")}
            self.reply(update_id, f"{load_settings().house_label} · весь дом\nКвартир: {len(inventory)}\nС газом: {counts['Gas']}\nС электричеством: {counts['Electricity']}\nС водой: {counts['Water']}\nИсточник: {self.store.get('inventory_source', 'ещё не загружен')}\nЭто покрытие API, не проверка физического наличия приборов.")
        elif command in ("/cases", "/candidates"):
            self._list(update_id, command == "/candidates", 0, now)
        elif command == "/demo":
            from .collector import demo_snapshot
            self.store.open_incident(f"demo:{update_id}", "Учебная квартира · вымышленные данные", "no_data", "Тест: газовые данные не поступают. Не относится к реальному прибору.", demo=True, meter_snapshot=demo_snapshot(now), now=now)
        elif command == "/scan":
            started = self.scan() if self.scan else False
            self.reply(update_id, "Обновление всего дома запущено. Прогресс: /status. Сбор займёт несколько минут." if started else "Сбор уже идёт или недоступен. Прогресс: /status.")
        elif command == "/history" and argument.isdecimal():
            self._history(update_id, command_number(argument))
        elif command == "/delivery":
            jobs = self.store.uncertain_jobs()
            text = "Неопределённые отправки:\n" + "\n".join(f"#{j['id']} · {j['kind']} · {j['key']}" for j in jobs)
            self.reply(update_id, text if jobs else "Неопределённых отправок нет.")
        elif command == "/resolve" and re.fullmatch(r"\d+ (sent \d+|retry)", argument):
            job_id, operation, *rest = argument.split()
            job_id = command_number(job_id)
            message_id = command_number(rest[0]) if operation == "sent" else None
            job = self.store.db.execute("SELECT * FROM outbox WHERE id=? AND state='unknown'", (job_id,)).fetchone()
            if job and __import__("json").loads(job["payload"]).get("chat_id", self.store.get("group_id")) == self.chat_id and __import__("json").loads(job["payload"]).get("group_generation") == self.store.get("group_generation"):
                if operation == "retry":
                    self.store.job_state(job_id, "pending")
                    self.reply(update_id, "Повтор отправки запрошен после вашей сверки.")
                else:
                    import json
                    payload = json.loads(job["payload"])
                    self.store.job_state(job_id, "sent", message_id=message_id)
                    if job["kind"] == "card":
                        self.store.set_incident_message_id(payload["incident_id"], message_id)
                    elif job["kind"] == "prompt":
                        self.store.db.execute("INSERT OR REPLACE INTO chat_replies VALUES (?,?,?,?,?,?)", (self.chat_id, message_id, payload["incident_id"], payload["version"], payload["action"], payload["actor_id"]))
                    self.reply(update_id, "Отправка отмечена доставленной.")
            else:
                self.reply(update_id, "Такая неопределённая отправка не найдена.")
        else:
            self.reply(update_id, "Команда не распознана. /help — список команд. Пояснение к проверке отправляйте ответом на приглашение бота.")

    def _callback(self, update_id, callback, now):
        data = callback.get("data", "")
        actor = callback.get("from", {})
        if not isinstance(actor.get("id"), int):
            return
        match = re.fullmatch(r"case:(\d+):(\d+):(claim|fault|unknown|normal|repaired)", data)
        if match:
            incident_id, version = map(command_number, match.group(1, 2))
            action = match.group(3)
            item = self.store.incident(incident_id)
            if not item or item["message_id"] != callback["message"].get("message_id") or item["version"] != version or item["state"] != "open":
                self.reply(update_id, "Эта карточка устарела. Откройте актуальные проверки: /cases.")
                return
            if action == "claim":
                self.store.transition(incident_id, version, action, actor["id"], self._name(actor), now=now)
            else:
                self.store.enqueue(f"prompt:{update_id}", "prompt", {"text": f"✍️ <b>Случай #{incident_id}</b>\nОпишите причину или результат проверки ответом на это сообщение.", "parse_mode": "HTML", "incident_id": incident_id, "version": version, "action": action, "actor_id": actor["id"], "chat_id": self.chat_id, "reply_markup": {"force_reply": True, "selective": False}})
            return
        match = re.fullmatch(r"history:(\d+)", data)
        if match:
            self._history(update_id, command_number(match[1]))
            return
        match = re.fullmatch(r"(cases|candidates):(\d+)", data)
        if match:
            self._list(update_id, match[1] == "candidates", command_number(match[2], minimum=0), now)
            return
        match = re.fullmatch(r"open:(\d+)", data)
        if match:
            item = self.store.incident(command_number(match[1]))
            if item:
                self.store.enqueue(f"card-refresh:{update_id}", "card", {"incident_id": item["id"], "version": item["version"]})
            return
        match = re.fullmatch(r"candidate:([A-Za-z0-9_-]+)", data)
        if match:
            for candidate in self._candidates(now):
                if candidate["gas_id"] == match[1]:
                    self.store.open_incident(candidate["gas_id"], candidate["label"], "no_data", candidate["evidence"], meter_snapshot=candidate["meter_snapshot"], now=now)
                    self.reply(update_id, "Ручная проверка создана или уже существует. /cases — открытые проверки.")
                    return

    def _candidates(self, now):
        from .collector import stale_candidates
        return stale_candidates(self.store.get("inventory", []), now)

    def _history(self, update_id, incident_id):
        events = self.store.history(incident_id)
        text = "\n\n".join(f"🕒 {local_time(e['at'])} · {e['actor']}\n{ACTIONS.get(e['action'], e['action'])}\n{evidence_text(e['reason']) if e['action'] == 'created' else e['reason']}" for e in events)
        for index, part in enumerate(text_parts(text or "Случай не найден.")):
            self.store.enqueue(f"history:{update_id}:{index}", "message", {"text": f"📜 <b>История случая #{incident_id}</b>\n\n{escape(part)}", "parse_mode": "HTML", "reply_markup": keyboard(), "chat_id": self.chat_id, "actor_id": self.actor_id})

    def _list(self, update_id, candidates, page, now):
        items = self._candidates(now) if candidates else self.store.incidents()
        page = min(page, max(0, (len(items) - 1) // 8))
        selected = items[page * 8:(page + 1) * 8]
        heading = "🔎 Кандидаты на ручную проверку" if candidates else "📋 Открытые случаи"
        rows, lines = [], [f"<b>{heading}</b>", f"Всего: {len(items)} · страница {page + 1}"]
        if candidates:
            lines.append("Причина не установлена.")
        for item in selected:
            if candidates:
                lines.append(f"\n🏠 <b>{display_field(item['label'], 200)}</b>\n🕒 {item['last_at']}")
                rows.append([{"text": "Проверить · " + item["label"], "callback_data": "candidate:" + item["gas_id"]}])
            else:
                lines.append(f"\n<b>#{item['id']} · {'🧪 ' if item['demo'] else ''}{display_field(item['label'], 200)}</b>\n🚩 {CASE_REASONS.get(item['kind'], 'Основание требует уточнения')}\n👤 {display_field(item['assignee'] or 'не назначен', 100)}")
                rows.append([{"text": f"Открыть #{item['id']}", "callback_data": f"open:{item['id']}"}])
        navigation = []
        prefix = "candidates" if candidates else "cases"
        if page:
            navigation.append({"text": "←", "callback_data": f"{prefix}:{page-1}"})
        if len(items) > (page + 1) * 8:
            navigation.append({"text": "→", "callback_data": f"{prefix}:{page+1}"})
        if navigation:
            rows.append(navigation)
        self.reply(update_id, "\n".join(lines), markup={"inline_keyboard": rows}, formatted=True)

    def _name(self, actor):
        employee = Access(self.store).employee(actor["id"])
        if employee:
            return employee["name"]
        return " ".join(str(actor.get(k, "")) for k in ("first_name", "last_name")).strip()[:100] or str(actor["id"])
