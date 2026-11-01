"""Employee invitations and access control shared by the bot and web admin."""

import hashlib
import hmac
import re
import secrets
from datetime import datetime, timezone


def initialize(store):
    store.db.executescript("""
        CREATE TABLE IF NOT EXISTS employees (
            id INTEGER PRIMARY KEY, name TEXT NOT NULL, phone TEXT NOT NULL UNIQUE,
            active INTEGER NOT NULL DEFAULT 1, telegram_id INTEGER UNIQUE,
            invite_hash TEXT, invite_until REAL, registered_at TEXT
        );
        CREATE TABLE IF NOT EXISTS registration_contacts (
            telegram_id INTEGER PRIMARY KEY, phone TEXT NOT NULL, expires REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS access_attempts (
            key TEXT PRIMARY KEY, count INTEGER NOT NULL, until REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS access_audit (
            id INTEGER PRIMARY KEY, actor TEXT NOT NULL, action TEXT NOT NULL,
            subject TEXT NOT NULL, at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS chat_replies (
            chat_id INTEGER NOT NULL, message_id INTEGER NOT NULL,
            incident_id INTEGER NOT NULL, version INTEGER NOT NULL,
            action TEXT NOT NULL, actor_id INTEGER NOT NULL,
            PRIMARY KEY(chat_id,message_id)
        );
    """)


def phone_number(value):
    value = re.sub(r"[\s()\-]", "", str(value))
    if re.fullmatch(r"8[0-9]{10}", value):
        value = "+7" + value[1:]
    if not re.fullmatch(r"\+[1-9][0-9]{7,14}", value):
        raise ValueError("Укажите телефон в международном формате, например +12025550101.")
    return value


def code_hash(code):
    return hashlib.sha256(str(code).replace("-", "").strip().upper().encode()).hexdigest()


def new_code():
    raw = "".join(secrets.choice("23456789ABCDEFGHJKMNPQRSTUVWXYZ") for _ in range(16))
    return "-".join(raw[i:i + 4] for i in range(0, 16, 4))


class Access:
    def __init__(self, store):
        self.store = store

    def audit(self, actor, action, subject):
        self.store.db.execute("INSERT INTO access_audit(actor,action,subject,at) VALUES (?,?,?,?)",
                              (str(actor), action, str(subject), datetime.now(timezone.utc).isoformat()))

    def employee(self, telegram_id):
        row = self.store.db.execute("SELECT * FROM employees WHERE telegram_id=? AND active=1", (telegram_id,)).fetchone()
        return dict(row) if row else None

    def employees(self):
        return [dict(r) for r in self.store.db.execute("SELECT * FROM employees ORDER BY name,id")]

    def create(self, name, phone, actor="admin"):
        if not str(name).strip() or len(str(name)) > 100:
            raise ValueError("Укажите имя сотрудника длиной до 100 символов.")
        with self.store.atomic():
            cursor = self.store.db.execute("INSERT INTO employees(name,phone) VALUES (?,?)", (str(name).strip(), phone_number(phone)))
            self.audit(actor, "employee_created", cursor.lastrowid)
            return cursor.lastrowid, self.invite(cursor.lastrowid, actor=actor)

    def invite(self, employee_id, *, actor="admin", now=None, rebind=False):
        now = now or datetime.now(timezone.utc)
        with self.store.atomic():
            row = self.store.db.execute("SELECT * FROM employees WHERE id=?", (employee_id,)).fetchone()
            if not row:
                raise ValueError("Сотрудник не найден.")
            if row["telegram_id"] is not None and not rebind:
                raise ValueError("Сотрудник уже зарегистрирован. Используйте перепривязку.")
            code = new_code()
            self.store.db.execute("UPDATE employees SET active=1,telegram_id=NULL,registered_at=NULL,invite_hash=?,invite_until=? WHERE id=?",
                                  (code_hash(code), now.timestamp() + 86400, employee_id))
            self.audit(actor, "employee_rebind" if rebind else "invitation_issued", employee_id)
            return code

    def disable(self, employee_id, actor="admin"):
        with self.store.atomic():
            self.store.db.execute("UPDATE employees SET active=0,invite_hash=NULL,invite_until=NULL WHERE id=?", (employee_id,))
            self.audit(actor, "employee_disabled", employee_id)

    def limited(self, key, now, limit=5):
        row = self.store.db.execute("SELECT * FROM access_attempts WHERE key=?", (key,)).fetchone()
        return bool(row and row["until"] > now.timestamp() and row["count"] >= limit)

    def attempt(self, key, now, window=900):
        self.store.db.execute("""INSERT INTO access_attempts VALUES (?,1,?) ON CONFLICT(key) DO UPDATE SET
            count=CASE WHEN until<=? THEN 1 ELSE count+1 END,
            until=CASE WHEN until<=? THEN excluded.until ELSE until END""",
            (key, now.timestamp() + window, now.timestamp(), now.timestamp()))

    def register(self, telegram_id, code, now, *, hashed=False):
        with self.store.atomic():
            key = f"register:{telegram_id}"
            if self.limited(key, now) or self.limited("register:global", now, 100):
                return False
            self.attempt(key, now)
            self.attempt("register:global", now)
            contact = self.store.db.execute("SELECT * FROM registration_contacts WHERE telegram_id=? AND expires>?", (telegram_id, now.timestamp())).fetchone()
            if not contact or self.employee(telegram_id):
                return False
            row = self.store.db.execute("SELECT * FROM employees WHERE phone=? AND active=1 AND telegram_id IS NULL", (contact["phone"],)).fetchone()
            if not row or not row["invite_hash"] or (row["invite_until"] or 0) <= now.timestamp():
                return False
            if len(code) > 64 or not hmac.compare_digest(row["invite_hash"], code if hashed else code_hash(code)):
                return False
            self.store.db.execute("UPDATE employees SET telegram_id=?,registered_at=?,invite_hash=NULL,invite_until=NULL WHERE id=?", (telegram_id, now.isoformat(), row["id"]))
            self.store.db.execute("DELETE FROM registration_contacts WHERE telegram_id=?", (telegram_id,))
            self.audit(telegram_id, "employee_registered", row["id"])
            return True

    def remember_contact(self, telegram_id, contact, now):
        if type(contact.get("user_id")) is not int or contact["user_id"] != telegram_id:
            return False
        try:
            # Telegram sometimes supplies an international number without '+'.
            number = str(contact.get("phone_number", ""))
            phone = phone_number(number if number.startswith("+") else "+" + number)
        except ValueError:
            return False
        self.store.db.execute("INSERT OR REPLACE INTO registration_contacts VALUES (?,?,?)", (telegram_id, phone, now.timestamp() + 900))
        return True

    def group_invite(self, actor="admin", now=None):
        now = now or datetime.now(timezone.utc)
        with self.store.atomic():
            if self.store.get("group_id") is not None:
                raise ValueError("Сначала отключите текущую группу.")
            code = new_code()
            self.store.set("group_invitation", {"hash": code_hash(code), "until": now.timestamp() + 86400})
            self.audit(actor, "group_invitation_issued", "group")
            return code

    def connect_group(self, chat_id, code, actor, now, *, hashed=False):
        with self.store.atomic():
            key = f"connect:{actor}"
            if self.limited(key, now) or self.limited("connect:global", now, 100):
                return False
            self.attempt(key, now)
            self.attempt("connect:global", now)
            invite = self.store.get("group_invitation") or {}
            if not self.employee(actor) or self.store.get("group_id") is not None or not invite or invite["until"] <= now.timestamp():
                return False
            if len(code) > 64 or not hmac.compare_digest(invite["hash"], code if hashed else code_hash(code)):
                return False
            self.store.set("group_id", chat_id)
            self.store.set("group_generation", secrets.token_hex(16))
            self.store.set("group_invitation", None)
            self.store.set("group_delivery_error", None)
            self.store.set("navigation_group", None)
            # IDs of messages in the old private chat cannot be edited in the group.
            self.store.db.execute("UPDATE incidents SET message_id=NULL")
            self.store.db.execute("DELETE FROM chat_replies")
            for item in self.store.incidents():
                self.store.enqueue(f"group-card:{chat_id}:{item['id']}:{now.timestamp()}", "card", {"incident_id": item["id"], "version": item["version"]})
            self.audit(actor, "group_connected", chat_id)
            return True

    def disconnect_group(self, actor="admin"):
        with self.store.atomic():
            self.audit(actor, "group_disconnected", self.store.get("group_id"))
            self.store.set("group_id", None)
            self.store.set("group_generation", None)
            self.store.set("group_invitation", None)
            self.store.db.execute("DELETE FROM chat_replies")
